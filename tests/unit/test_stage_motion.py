# -*- coding: utf-8 -*-
"""Unit tests for set_stage_motion in MS_zenapi_stage_LM (gRPC layer MOCKED).

Asserts both set_acceleration and set_speed are issued once with the expected
per-axis percents, that None falls back to the module-default constants, and that
the returned read-back reflects the values that were set. Also covers the
per-parameter failure handling confirmed on the real gateway (2026-07-14): a
SetSpeed/GetSpeed rejection is tolerated (speed_x/speed_y come back None) since
acceleration is the parameter that actually matters for protecting a live
sample, while an acceleration failure DOES propagate.
"""
import asyncio
import contextlib

import pytest

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
pytest.importorskip("zen_api", reason="zen_api not resolvable on this machine")

import MS_zenapi_stage_LM as stage              # noqa: E402


@contextlib.asynccontextmanager
async def _fake_channel(cfg):
    yield (None, None)


class _Resp:
    def __init__(self, x, y):
        self.x = x
        self.y = y


def _make_stub(rec):
    class _Stub:
        def __init__(self, channel=None, metadata=None):
            pass

        async def set_speed(self, req):
            rec["speed"].append((req.speed_x, req.speed_y))

        async def set_acceleration(self, req):
            rec["accel"].append((req.acceleration_x, req.acceleration_y))

        async def get_speed(self, req):
            return _Resp(*rec["speed"][-1])

        async def get_acceleration(self, req):
            return _Resp(*rec["accel"][-1])

    return _Stub


def test_set_stage_motion_explicit(monkeypatch):
    rec = {"speed": [], "accel": []}
    monkeypatch.setattr(stage, "open_zen_channel", _fake_channel)
    monkeypatch.setattr(stage, "StageServiceStub", _make_stub(rec))

    out = asyncio.run(stage.set_stage_motion(speed_percent=60, acceleration_percent=40))

    # Both set once, both axes, with the requested percents.
    assert rec["speed"] == [(60, 60)]
    assert rec["accel"] == [(40, 40)]
    assert out == {"speed_x": 60, "speed_y": 60,
                   "acceleration_x": 40, "acceleration_y": 40}


def test_set_stage_motion_defaults_to_constants(monkeypatch):
    rec = {"speed": [], "accel": []}
    monkeypatch.setattr(stage, "open_zen_channel", _fake_channel)
    monkeypatch.setattr(stage, "StageServiceStub", _make_stub(rec))

    asyncio.run(stage.set_stage_motion())   # None, None → module constants

    assert rec["speed"] == [(stage.STAGE_TRAVEL_SPEED_PERCENT,
                             stage.STAGE_TRAVEL_SPEED_PERCENT)]
    assert rec["accel"] == [(stage.STAGE_ACCELERATION_PERCENT,
                             stage.STAGE_ACCELERATION_PERCENT)]


def _make_stub_speed_rejected(rec):
    """Stub matching the real gateway: SetSpeed/GetSpeed always reject; only
    SetAcceleration/GetAcceleration succeed."""
    class _Stub:
        def __init__(self, channel=None, metadata=None):
            pass

        async def set_speed(self, req):
            raise RuntimeError('FAILED_PRECONDITION: "This parameter is not supported by the device."')

        async def get_speed(self, req):
            raise RuntimeError('FAILED_PRECONDITION: "This parameter is not supported by the device."')

        async def set_acceleration(self, req):
            rec["accel"].append((req.acceleration_x, req.acceleration_y))

        async def get_acceleration(self, req):
            return _Resp(*rec["accel"][-1])

    return _Stub


def test_set_stage_motion_tolerates_speed_rejection(monkeypatch):
    # Confirmed on the real gateway (2026-07-14): SetSpeed/GetSpeed reject
    # outright while SetAcceleration/GetAcceleration work fine. A speed failure
    # must not prevent acceleration from being applied and reported.
    rec = {"accel": []}
    monkeypatch.setattr(stage, "open_zen_channel", _fake_channel)
    monkeypatch.setattr(stage, "StageServiceStub", _make_stub_speed_rejected(rec))

    out = asyncio.run(stage.set_stage_motion(speed_percent=60, acceleration_percent=40))

    assert rec["accel"] == [(40, 40)]
    assert out == {"speed_x": None, "speed_y": None,
                   "acceleration_x": 40, "acceleration_y": 40}


def test_set_stage_motion_acceleration_failure_raises(monkeypatch):
    # Unlike a speed failure, an acceleration failure DOES propagate: it is the
    # one parameter this function exists to guarantee (see module docstring).
    class _Stub:
        def __init__(self, channel=None, metadata=None):
            pass

        async def set_acceleration(self, req):
            raise RuntimeError("gateway unreachable")

    monkeypatch.setattr(stage, "open_zen_channel", _fake_channel)
    monkeypatch.setattr(stage, "StageServiceStub", _Stub)

    with pytest.raises(RuntimeError):
        asyncio.run(stage.set_stage_motion())
