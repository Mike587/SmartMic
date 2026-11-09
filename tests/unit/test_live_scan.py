# -*- coding: utf-8 -*-
"""Unit tests for the read-only LiveScanService probe.

MS_zenapi_live_scan.get_live_scan_configuration must map every field of the
LiveScanServiceConfiguration message into a plain dict, and must not swallow a
gateway rejection. The module must not expose load / eject / set helpers. The
gRPC layer is MOCKED.
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


def test_probe_exposes_no_moving_calls():
    # Load/eject/set are untested on the CD7 and must not sneak in here.
    public = {n for n in dir(ls) if not n.startswith("_")}
    for forbidden in ("eject", "load_tray", "set_live_scan", "set_configuration"):
        assert not any(forbidden in n.lower() for n in public), forbidden
