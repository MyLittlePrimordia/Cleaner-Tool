"""U-005: the driver store - what is in it, and what may never be deleted.

The dangerous part of this tool is not the deleting; it is deciding WHICH
packages are safe. An unplugged device's only driver is "not in use" too, so
these tests pin the two rules that make the offer defensible:

    * a package is only offered when a sibling with the same vendor INF is
      currently bound to a device (the previous version of a driver in use);
    * every delete is preceded by an export whose exit code was checked, in
      the same elevated script, with no /force anywhere.
"""
from __future__ import annotations

import pytest

from app.drivers import store

GB = 1024 ** 3

EN = """Microsoft PnP Utility

Published Name:     oem12.inf
Original Name:      nvlt.inf
Provider Name:      NVIDIA
Class Name:         Display adapters
Class GUID:         {4d36e968-e325-11ce-bfc1-08002be10318}
Driver Version:     10/27/2023 31.0.15.3699
Signer Name:        Microsoft Windows Hardware Compatibility Publisher

Published Name:     oem3.inf
Original Name:      nvlt.inf
Provider Name:      NVIDIA
Class Name:         Display adapters
Class GUID:         {4d36e968-e325-11ce-bfc1-08002be10318}
Driver Version:     03/02/2022 30.0.14.1234
Signer Name:        Microsoft Windows Hardware Compatibility Publisher

Published Name:     oem7.inf
Original Name:      usbvideo.inf
Provider Name:      Logitech
Class Name:         Cameras
Class GUID:         {ca3e7ab9-b4c3-4ae6-8251-579ef933890f}
Driver Version:     01/02/2020 1.0.0.5
Signer Name:        Logitech
"""

#: The same output with localized labels and a DD.MM.YYYY date. The parser is
#: shape-based, so nothing about the language may change the result.
DE = """Microsoft PnP Utility

Ver\u00f6ffentlichter Name: oem12.inf
Urspr\u00fcnglicher Name:  nvlt.inf
Anbietername:        NVIDIA
Klassenname:         Displayadapter
Klassen-GUID:        {4d36e968-e325-11ce-bfc1-08002be10318}
Treiberversion:      27.10.2023 31.0.15.3699
Signaturname:        Microsoft Windows Hardware Compatibility Publisher
"""


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

def test_parses_the_fields_a_cleanup_needs():
    pkgs = store.parse_enum(EN)
    assert [p.published for p in pkgs] == ["oem12.inf", "oem3.inf", "oem7.inf"]
    first = pkgs[0]
    assert first.original == "nvlt.inf"
    assert first.provider == "NVIDIA"
    assert first.cls == "Display adapters"
    assert first.version == "10/27/2023 31.0.15.3699"
    assert "Hardware Compatibility" in first.signer
    assert first.label == "nvlt.inf"


def test_localized_labels_and_dates_parse_identically():
    pkg = store.parse_enum(DE)[0]
    assert pkg.published == "oem12.inf"
    assert pkg.original == "nvlt.inf"
    assert pkg.provider == "NVIDIA"
    assert pkg.cls == "Displayadapter"
    assert pkg.version == "27.10.2023 31.0.15.3699"


def test_blocks_without_a_blank_line_between_them_still_split():
    text = ("Published Name: oem1.inf\nOriginal Name: a.inf\n"
            "Provider Name: A\n"
            "Published Name: oem2.inf\nOriginal Name: b.inf\n"
            "Provider Name: B\n")
    pkgs = store.parse_enum(text)
    assert [p.published for p in pkgs] == ["oem1.inf", "oem2.inf"]
    assert [p.provider for p in pkgs] == ["A", "B"]


def test_a_header_or_junk_block_is_skipped_not_guessed_at():
    text = ("Microsoft PnP Utility\n\n"
            "Some: unrelated line\nAnother: one\n\n"
            "Published Name: oem9.inf\n")
    pkgs = store.parse_enum(text)
    assert [p.published for p in pkgs] == ["oem9.inf"]
    assert pkgs[0].provider == ""


@pytest.mark.parametrize("junk", ["", None, 5, "\n\n", "Published Name:"])
def test_parse_survives_junk(junk):
    assert store.parse_enum(junk) == []


def test_a_guid_never_masquerades_as_a_value():
    pkg = store.parse_enum(EN)[0]
    assert "{" not in pkg.provider
    assert "{" not in pkg.cls


#: Windows 11's pnputil prints two extra fields after the signer. Neither may
#: end up in a display slot.
MODERN = """Microsoft PnP Utility

Published Name:     oem61.inf
Original Name:      amdacpbus.inf
Provider Name:      AMD
Class Name:         System
Class GUID:         {4d36e97d-e325-11ce-bfc1-08002be10318}
Driver Version:     06/17/2026 6.2620.0.0
Signer Name:        Microsoft Windows Hardware Compatibility Publisher
Attributes:         Universal
WHCP Version:       Unknown
"""


def test_extra_modern_fields_do_not_become_a_provider_class_or_signer():
    pkg = store.parse_enum(MODERN)[0]
    assert pkg.provider == "AMD"
    assert pkg.cls == "System"
    assert pkg.version == "06/17/2026 6.2620.0.0"
    # Field order alone cannot separate the signer from "Universal" here, so
    # the slot stays empty rather than holding a confident lie.
    assert pkg.signer == ""


# --------------------------------------------------------------------------- #
# in use / safe to offer
# --------------------------------------------------------------------------- #

def test_in_use_reads_the_inf_each_device_is_bound_to():
    devices = [{"inf": "oem12.inf"}, {"inf": "OEM3.INF"}, {"inf": ""}, {}]
    assert store.in_use(devices) == {"oem12.inf", "oem3.inf"}


def test_in_use_survives_junk_devices():
    assert store.in_use([None, 5, {"inf": None}]) == set()


def test_only_a_superseded_version_is_offered():
    pkgs = store.parse_enum(EN)
    rows = {r.package.published: r for r in store.assess(pkgs, {"oem12.inf"})}
    assert rows["oem12.inf"].state == "in-use"
    assert rows["oem3.inf"].state == "superseded"
    assert rows["oem3.inf"].offered is True
    assert "oem12.inf" in rows["oem3.inf"].why


def test_an_unused_package_whose_replacement_is_not_installed_is_kept():
    """The unplugged-camera case: not in use, so tempting, and the only copy
    of that driver on the PC. Deleting it would make Windows ask for it the
    next time the device is connected."""
    pkgs = store.parse_enum(EN)
    rows = store.assess(pkgs, set())
    assert [r.state for r in rows] == ["orphaned"] * 3
    assert store.reclaimable(rows) == []
    assert all("not offered" in r.why for r in rows)


def test_a_package_in_use_is_never_reclaimable_even_with_a_sibling():
    pkgs = store.parse_enum(EN)
    rows = store.assess(pkgs, {"oem12.inf", "oem3.inf"})
    assert store.reclaimable(rows) == []


def test_two_unused_versions_are_both_read_only():
    pkgs = [store.Package("oem1.inf", "same.inf"),
            store.Package("oem2.inf", "same.inf")]
    rows = store.assess(pkgs, set())
    assert {r.state for r in rows} == {"orphaned"}


def test_a_package_with_no_original_name_is_never_superseded():
    """Without a vendor name there is no way to prove a sibling exists."""
    pkgs = [store.Package("oem1.inf"), store.Package("oem2.inf")]
    assert store.reclaimable(store.assess(pkgs, {"oem1.inf"})) == []


def test_offered_rows_sort_first_and_versions_sit_together():
    pkgs = store.parse_enum(EN) + [store.Package("oem4.inf", "other.inf")]
    rows = store.assess(pkgs, {"oem12.inf"})
    assert [r.state for r in rows][0] == "superseded"
    assert [r.package.published for r in rows][:2] == ["oem3.inf", "oem4.inf"]


# --------------------------------------------------------------------------- #
# export before delete
# --------------------------------------------------------------------------- #

def _mini(two=True):
    pkgs = [store.Package("oem12.inf", "nvlt.inf",
                          version="10/27/2023 31.0.15.3699")]
    if two:
        pkgs.append(store.Package("oem3.inf", "nvlt.inf",
                                  version="03/02/2022 30.0.14.1234"))
    return pkgs


def test_the_script_exports_before_it_deletes_and_never_forces():
    script = store.build_clean_script(_mini(), r"C:\Drivers\backup\x")
    export_at = script.index("/export-driver 'oem3.inf'")
    delete_at = script.index("/delete-driver 'oem3.inf'")
    assert export_at < delete_at
    assert "/force" not in script.lower()
    # the delete only runs on the success branch
    ok_at = script.index("if ($LASTEXITCODE -eq 0) { $ok = $true }")
    assert ok_at < export_at + 1 or ok_at < delete_at
    assert "if ($ok) {" in script
    assert "EXPORT_FAILED" in script
    assert store.DONE in script.strip().splitlines()[-1]


def test_the_script_creates_the_folder_and_skips_non_oem_names():
    pkgs = _mini() + [store.Package("usbvideo.inf", "usbvideo.inf")]
    script = store.build_clean_script(pkgs, r"C:\Backup")
    assert "New-Item -ItemType Directory -Force -Path 'C:\\Backup'" in script
    assert "usbvideo.inf" not in script
    assert script.count("/export-driver") == 2


def test_quotes_in_a_path_cannot_break_out_of_the_script():
    script = store.build_clean_script(_mini(False), "C:\\it's here")
    assert "'C:\\it''s here'" in script


def test_output_tokens_are_read_per_package():
    text = ("CLEANER_STORE oem3.inf REMOVED\n"
            "CLEANER_STORE oem4.inf DELETE_FAILED 50\n"
            "CLEANER_STORE oem5.inf EXPORT_FAILED\n"
            + store.DONE)
    parsed = store.parse_clean_output(text)
    assert parsed["removed"] == ["oem3.inf"]
    assert [p for p, _r in parsed["failed"]] == ["oem4.inf", "oem5.inf"]
    assert parsed["complete"] is True


def test_a_truncated_run_is_not_reported_as_complete():
    parsed = store.parse_clean_output("CLEANER_STORE oem3.inf REMOVED")
    assert parsed["removed"] == ["oem3.inf"]
    assert parsed["complete"] is False


def test_clean_reports_declined_elevation_as_cancelled_not_failed():
    res = store.clean_elevated(_mini(), r"C:\b", stamp="s",
                               runner=lambda _s, _t: (False, ""))
    assert res["cancelled"] is True
    assert res["error"] == "" and res["removed"] == []


def test_clean_only_removes_what_the_export_let_through():
    seen = {}

    def runner(script, _timeout):
        seen["script"] = script
        return True, ("CLEANER_STORE oem3.inf REMOVED\n"
                      "CLEANER_STORE oem12.inf EXPORT_FAILED\n" + store.DONE)

    res = store.clean_elevated(_mini(), r"C:\b", stamp="2026-10-27_1200",
                               runner=runner)
    assert res["removed"] == ["oem3.inf"]
    assert [p for p, _r in res["failed"]] == ["oem12.inf"]
    assert res["folder"] == r"C:\b\2026-10-27_1200"
    assert "2026-10-27_1200" in seen["script"]
    assert res["complete"] is True


def test_clean_rejects_an_empty_or_non_oem_selection():
    assert store.clean_elevated([], r"C:\b")["error"]
    junk = [store.Package("usbvideo.inf", "usbvideo.inf")]
    assert store.clean_elevated(junk, r"C:\b")["error"]


def test_a_clean_that_never_finished_says_nothing_was_changed():
    res = store.clean_elevated(_mini(), r"C:\b", stamp="s",
                               runner=lambda _s, _t: (True, ""))
    assert res["removed"] == []
    assert "Nothing was changed" in res["error"]


def test_a_runner_that_raises_becomes_an_error_string():
    def boom(_script, _timeout):
        raise OSError("no powershell")

    res = store.clean_elevated(_mini(), r"C:\b", stamp="s", runner=boom)
    assert res["error"] and not res["cancelled"]


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #

def test_report_reads_devices_and_the_store_together():
    import json

    device = {"id": "PCI\\VEN_10DE", "name": "NVIDIA GPU",
              "cls": "Display", "version": "31.0.15.3699",
              "inf": "oem12.inf", "code": 0}
    lines = ["##JSON##" + json.dumps([device])]
    res = store.report(run_powershell=lambda _s, _t: lines,
                       runner=lambda _a, _t: (0, EN))
    assert res["error"] == ""
    assert res["packages"] == 3
    assert res["used"] == 1
    assert [r.state for r in res["rows"]].count("superseded") == 1


def test_report_explains_a_failed_store_query():
    res = store.report(run_powershell=lambda _s, _t: ["##JSON##[]"],
                       runner=lambda _a, _t: (5, ""))
    assert "exit code 5" in res["error"]
    assert res["rows"] == []


def test_report_explains_a_broken_inventory():
    res = store.report(run_powershell=lambda _s, _t: ["not json"],
                       runner=lambda _a, _t: (0, EN))
    assert "devices" in res["error"]


def test_report_survives_a_runner_that_cannot_start():
    def boom(_argv, _timeout):
        raise FileNotFoundError("pnputil")

    res = store.report(run_powershell=lambda _s, _t: ["##JSON##[]"],
                       runner=boom)
    assert "driver store" in res["error"].lower()


# --------------------------------------------------------------------------- #
# reachability
# --------------------------------------------------------------------------- #

def test_the_reclaim_plan_route_reaches_the_driver_store():
    """A plan row that names a screen has to reach that screen.

    Before U-005 this row pointed at Drive Toolkit, which has no driver-store
    view at all - the same dead-end the routes table exists to prevent.
    """
    from app import reclaim, routes

    plan = reclaim.build_plan([])
    row = next(s for s in plan["steps"] if "driver" in s["title"].lower())
    assert row["target"] == "driver_store"
    route = routes.get("driver_store")
    assert route is not None
    assert route.label == "Open Driver store"

    class App:
        def __init__(self):
            self.opened = []

        def _open_driver_store(self):
            self.opened.append(True)

    app = App()
    assert routes.open_route(app, "driver_store") is True
    assert app.opened == [True]


def test_the_drivers_tab_opens_the_store_and_refuses_mid_run(monkeypatch):
    from _tkhelpers import make_root
    from app.ui.tabs.driverstab import DriversTab

    opened = []
    import app.ui.dialogs.driver_store_dialog as mod

    class _Stub:
        def __init__(self, *_a, **_k):
            opened.append(True)

        def wait(self):
            pass

    monkeypatch.setattr(mod, "DriverStoreDialog", _Stub, raising=True)
    root = make_root()
    root.withdraw()
    try:
        tab = DriversTab(root, object())
        tab._open_store()
        assert opened == [True]
        tab.set_run_enabled(False)     # a run owns the app
        tab._open_store()
        assert opened == [True], "a run in progress must block it"
        tab.set_run_enabled(True)
        assert tab.store_btn._enabled is True
    finally:
        root.destroy()


# --------------------------------------------------------------------------- #
# the dialog
# --------------------------------------------------------------------------- #

def _dialog(monkeypatch, data):
    from _tkhelpers import make_root
    from app.ui.dialogs.driver_store_dialog import DriverStoreDialog

    monkeypatch.setattr(DriverStoreDialog, "_start",
                        lambda self, note=None: None, raising=True)
    root = make_root()
    root.withdraw()
    return root, DriverStoreDialog(root, object())


def _data(monkeypatch=None):
    pkgs = store.parse_enum(EN)
    rows = store.assess(pkgs, {"oem12.inf"})
    return {"rows": rows, "packages": 3, "used": 1, "error": ""}


def test_the_dialog_lists_only_what_can_be_acted_on(monkeypatch):
    root, dlg = _dialog(monkeypatch, None)
    try:
        dlg._land(_data())
        assert "1 old driver version" in dlg.banner._title_full
        rows = dlg.table._src
        keys = [r["_key"] for r in rows]
        assert "oem12.inf" not in keys, "an in-use package must not be listed"
        offered = next(r for r in rows if r["_key"] == "oem3.inf")
        assert offered["_check"] is False
        kept = next(r for r in rows if r["_key"] == "oem7.inf")
        assert "_check" not in kept
        assert "no device is using it" in kept["_cells"]["package"]["sub"]
    finally:
        dlg.close()
        root.destroy()


def test_the_action_needs_a_selection_and_names_the_count(monkeypatch):
    root, dlg = _dialog(monkeypatch, None)
    try:
        dlg._land(_data())
        assert dlg._primary._enabled is False
        row = next(r for r in dlg.table._src if r["_key"] == "oem3.inf")
        dlg._on_check(row)
        assert dlg._primary._enabled is True
        assert "(1)" in dlg._primary._text
        dlg._on_check(row)
        assert dlg._primary._enabled is False
    finally:
        dlg.close()
        root.destroy()


def test_removing_asks_first_and_hands_the_chosen_package_over(monkeypatch):
    import app.ui.dialogs.driver_store_dialog as mod

    root, dlg = _dialog(monkeypatch, None)
    try:
        dlg._land(_data())
        row = next(r for r in dlg.table._src if r["_key"] == "oem3.inf")
        dlg._on_check(row)
        monkeypatch.setattr(mod, "_themed_askyesno",
                            lambda *_a, **_k: True, raising=True)
        monkeypatch.setattr(dlg, "_drivers_root", lambda: r"C:\Drivers",
                            raising=False)
        clean_calls = []

        def fake_clean(picked, root_):
            clean_calls.append(([p.published for p in picked], root_))
            return {"folder": r"C:\Drivers\backup\s", "removed": ["oem3.inf"],
                    "failed": [], "cancelled": False, "error": "",
                    "complete": True}

        import app.drivers.store as store_mod
        monkeypatch.setattr(store_mod, "clean_elevated", fake_clean,
                            raising=True)
        dlg.run_worker = lambda fn, land: land(fn())
        landing = []
        dlg._start = lambda note=None: landing.append(note)
        dlg._clean()
        assert clean_calls == [(["oem3.inf"], r"C:\Drivers\backup")]
        assert landing and "Removed 1 package" in landing[0][0]
        assert "Drivers\\backup\\s" in landing[0][0]
    finally:
        dlg.close()
        root.destroy()


def test_a_declined_prompt_says_nothing_changed(monkeypatch):
    root, dlg = _dialog(monkeypatch, None)
    try:
        dlg._land(_data())
        dlg._land_clean({"cancelled": True})
        assert "declined" in dlg.status.caption.cget("text")
    finally:
        dlg.close()
        root.destroy()


def test_an_unreadable_store_is_explained_in_the_header(monkeypatch):
    root, dlg = _dialog(monkeypatch, None)
    try:
        dlg._land({"rows": [], "packages": 0, "used": 0,
                   "error": "Windows would not list the driver store."})
        assert "Could not read" in dlg.banner._title_full
        assert "would not list" in dlg.banner._sub_full
    finally:
        dlg.close()
        root.destroy()


def test_nothing_left_to_clean_is_a_success_state(monkeypatch):
    root, dlg = _dialog(monkeypatch, None)
    try:
        dlg._land({"rows": store.assess(store.parse_enum(EN), {"oem12.inf"}),
                   "packages": 3, "used": 1, "error": ""})
        dlg._land({"rows": [], "packages": 3, "used": 3, "error": ""})
        assert "No leftover driver versions" in dlg.banner._title_full
    finally:
        dlg.close()
        root.destroy()
