# -*- coding: utf-8 -*-
"""Unit tests for MS_zenapi_live_scan and the LoA tray wrappers.

get_live_scan_configuration must map every LiveScanServiceConfiguration field
into a plain dict and must not swallow a gateway rejection. The raw tray RPCs
send their request with a timeout. The LoA eject_tray / load_tray_and_prescan
refuse while busy, change from the 50x to the 5x before an eject, and reset a
DefiniteFocusTracker. SetConfiguration is not wrapped yet. Everything is MOCKED.
"""
import asyncio
import contextlib
import dataclasses

import pytest

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
pytest.importorskip("zen_api", reason="zen_api not resolvable on this machine")

from grpclib.const import Status                # noqa: E402
from grpclib.exceptions import GRPCError        # noqa: E402
from zen_api.lm.live_scan.v1 import (           # noqa: E402
    LiveScanServiceConfiguration,
    LiveScanServiceGetConfigurationResponse,
)

import MS_zenapi_live_scan as ls                # noqa: E402


@contextlib.asynccontextmanager
async def _fake_channel(cfg):
    yield (None, None)


def _stub_returning(response=None, error=None, calls=None):
    class _FakeStub:
        def __init__(self, channel=None, metadata=None):
            pass

        async def get_configuration(self, req):
            if calls is not None:
                calls.append("get_configuration")
            if error is not None:
                raise error
            return response

    return _FakeStub


def test_config_fields_match_proto():
    # If Zeiss adds/renames a field, this flags that CONFIG_FIELDS is stale.
    proto_fields = tuple(f.name for f in dataclasses.fields(LiveScanServiceConfiguration))
    assert ls.CONFIG_FIELDS == proto_fields


def test_get_configuration_maps_all_fields(monkeypatch):
    cfg = LiveScanServiceConfiguration(
        sample_holder_template="Multiwell 384",
        live_scan_detection=True,
        measure_bottom_thickness=True,
        determine_bottom_material=False,
        create_carrier_overview=True,
        read_barcodes=False,
        use_left_barcode=False,
        use_right_barcode=True,
        automatic_live_scan_calibration=True,
        refractive_index=1.52,
        material="Glass",
    )
    calls = []
    monkeypatch.setattr(ls, "open_zen_channel", _fake_channel)
    monkeypatch.setattr(ls, "LiveScanServiceStub", _stub_returning(
        LiveScanServiceGetConfigurationResponse(config=cfg), calls=calls))

    result = asyncio.run(ls.get_live_scan_configuration())

    assert calls == ["get_configuration"]
    assert result == {name: getattr(cfg, name) for name in ls.CONFIG_FIELDS}
    assert result["sample_holder_template"] == "Multiwell 384"
    assert result["refractive_index"] == 1.52


def test_get_configuration_does_not_swallow_rejection(monkeypatch):
    monkeypatch.setattr(ls, "open_zen_channel", _fake_channel)
    monkeypatch.setattr(ls, "LiveScanServiceStub", _stub_returning(
        error=GRPCError(Status.UNIMPLEMENTED, "not supported")))
    with pytest.raises(GRPCError):
        asyncio.run(ls.get_live_scan_configuration())


_CD7_CFG = {   # as read on the CD7, 2026-11-09
    "sample_holder_template": "Multiwell 6 - 250um.czsht",
    "live_scan_detection": False,
    "measure_bottom_thickness": True,
    "determine_bottom_material": False,
    "create_carrier_overview": False,
    "read_barcodes": False,
    "use_left_barcode": False,
    "use_right_barcode": True,
    "automatic_live_scan_calibration": False,
    "refractive_index": 1.594,
    "material": "Polystyrene",
}


def test_format_lists_on_and_off_steps():
    line = ls.format_live_scan_configuration(_CD7_CFG)
    assert "template 'Multiwell 6 - 250um.czsht'" in line
    assert "material Polystyrene, RI 1.594" in line
    assert "prescan on: measure_bottom_thickness, use_right_barcode" in line
    assert "off: live_scan_detection, determine_bottom_material," in line
    assert "MISMATCH" not in line   # no carrier given → no check


def test_format_matching_carrier_strips_czsht():
    line = ls.format_live_scan_configuration(_CD7_CFG, carrier="Multiwell 6 - 250um")
    assert "MISMATCH" not in line


def test_format_flags_mismatch():
    line = ls.format_live_scan_configuration(_CD7_CFG, carrier="Multichamber 384")
    assert line.endswith("MISMATCH: ZEN carrier is 'Multichamber 384'")


def test_format_tolerates_missing_keys():
    line = ls.format_live_scan_configuration({}, carrier="X")
    assert "prescan on: none" in line and "MISMATCH" not in line


def test_set_configuration_not_wrapped_yet():
    # Changing the prescan flags is a later step (TODO.md); keep it out until then.
    public = {n for n in dir(ls) if not n.startswith("_")}
    for forbidden in ("set_live_scan", "set_configuration"):
        assert not any(forbidden in n.lower() for n in public), forbidden


# --------------------------------------------------------------------------
# Raw tray RPCs
# --------------------------------------------------------------------------
class _Resp:
    def __init__(self, notifications):
        self.notifications = notifications


@pytest.fixture
def rpc_calls(monkeypatch):
    calls = []

    class _FakeStub:
        def __init__(self, channel=None, metadata=None):
            pass

        async def eject_tray(self, req, timeout=None):
            calls.append(("eject_tray", type(req).__name__, timeout))
            return _Resp(["ejected"])

        async def load_tray_and_prescan(self, req, timeout=None):
            calls.append(("load_tray_and_prescan", type(req).__name__, timeout))
            return _Resp([])

    monkeypatch.setattr(ls, "open_zen_channel", _fake_channel)
    monkeypatch.setattr(ls, "LiveScanServiceStub", _FakeStub)
    return calls


def test_eject_rpc_sends_request_with_timeout(rpc_calls):
    assert asyncio.run(ls.eject_tray()) == ["ejected"]
    assert rpc_calls == [("eject_tray", "LiveScanServiceEjectTrayRequest", ls.EJECT_TIMEOUT_S)]


def test_load_rpc_sends_request_with_timeout(rpc_calls):
    assert asyncio.run(ls.load_tray_and_prescan(timeout_s=5.0)) == []
    assert rpc_calls == [("load_tray_and_prescan",
                          "LiveScanServiceLoadTrayAndPrescanRequest", 5.0)]


# --------------------------------------------------------------------------
# LoA wrappers (guards)
# --------------------------------------------------------------------------
import MS_CD7_API_LoA as ms                    # noqa: E402
import MS_zenapi_stage_LM as stage             # noqa: E402

IMMERSION = stage.IMMERSION_OBJECTIVE_POSITION


@pytest.fixture
def loa(monkeypatch):
    """Mock the LoA layer; record the order of optics changes and tray RPCs."""
    ev = []
    state = {"busy": False, "optics": (3, 1)}

    async def fake_eject():
        ev.append("eject")
        return ["ejected"]

    async def fake_load():
        ev.append("load")
        state["optics"] = (2, state["optics"][1])   # ZEN changes to the 5x
        return []

    def fake_optics(obj, opt):
        ev.append(("optics", obj, opt))
        state["optics"] = (obj, opt)

    monkeypatch.setattr(ms, "is_microscope_busy", lambda: state["busy"])
    monkeypatch.setattr(ms, "get_current_objective_and_optovar", lambda: state["optics"])
    monkeypatch.setattr(ms, "set_optics_verified", fake_optics)
    monkeypatch.setattr(ms, "get_current_z_position", lambda: 7e-6)
    monkeypatch.setattr(ms, "get_sample_carrier_name", lambda: "PLATE")
    monkeypatch.setattr(ls, "eject_tray", fake_eject)
    monkeypatch.setattr(ls, "load_tray_and_prescan", fake_load)
    # The wait for the DF carrier thickness after a load is tested in test_df_carrier.py.
    monkeypatch.setattr(ms, "wait_for_df_bottom", lambda *a, **k: {})
    return ev, state


def test_eject_on_dry_objective_does_not_change_optics(loa):
    ev, state = loa
    assert ms.eject_tray() == ["ejected"]
    assert ev == ["eject"]


def test_eject_on_50x_changes_to_5x_first_keeping_optovar(loa):
    ev, state = loa
    state["optics"] = (IMMERSION, 2)
    ms.eject_tray()
    assert ev == [("optics", ms.EJECT_DRY_OBJECTIVE, 2), "eject"]


def test_eject_on_50x_without_switch_ejects_directly(loa):
    ev, state = loa
    state["optics"] = (IMMERSION, 1)
    ms.eject_tray(switch_to_dry_objective=False)
    assert ev == ["eject"]


def test_eject_not_sent_if_optics_change_fails(loa, monkeypatch):
    ev, state = loa
    state["optics"] = (IMMERSION, 1)

    def never_settles(obj, opt):
        raise RuntimeError("Optics never settled")

    monkeypatch.setattr(ms, "set_optics_verified", never_settles)
    with pytest.raises(RuntimeError, match="never settled"):
        ms.eject_tray()
    assert ev == []


@pytest.mark.parametrize("fn", ["eject_tray", "load_tray_and_prescan"])
def test_tray_moves_refused_while_busy(loa, fn):
    ev, state = loa
    state["busy"] = True
    state["optics"] = (IMMERSION, 1)
    with pytest.raises(RuntimeError, match="busy"):
        getattr(ms, fn)()
    assert ev == []   # no optics change, no tray RPC


@pytest.mark.parametrize("fn", ["eject_tray", "load_tray_and_prescan"])
def test_tray_moves_reset_focus_tracker(loa, fn):
    tracker = ms.DefiniteFocusTracker(surface_z_m=1.35e-3)
    getattr(ms, fn)(focus_tracker=tracker)
    assert tracker.last_surface_z_m is None


def test_load_sends_rpc_and_survives_unreadable_readback(loa, monkeypatch):
    ev, state = loa

    def boom():
        raise RuntimeError("gateway hiccup")

    monkeypatch.setattr(ms, "get_current_z_position", boom)
    assert ms.load_tray_and_prescan() == []   # the read-back is log only
    assert ev == ["load"]
