# -*- coding: utf-8 -*-
"""Unit tests for set_stage_motion in MS_zenapi_stage_LM (gRPC layer MOCKED).

Asserts both set_acceleration and set_speed are issued once with the expected
per-axis percents, that None falls back to the module-default constants, and that
the returned read-back reflects the values that were set.
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
