# -*- coding: utf-8 -*-
"""Unit tests for the immersion guard in MS_zenapi_stage_LM.

move_stage_to_new_xy_position lowers Z to 0 for collision safety, which breaks
the immersion bridge on this inverted CD7. With the immersion objective active it
must REFUSE the move (raise) and must NOT lower Z; with a dry objective it runs
the normal path. The gRPC layer is MOCKED so these run with no microscope.
"""
import asyncio
import contextlib

import pytest

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
pytest.importorskip("zen_api", reason="zen_api not resolvable on this machine")

import MS_zenapi_stage_LM as stage              # noqa: E402
import MS_zenapi_objectivechanger as oc         # noqa: E402
import MS_zenapi_focus as focus                  # noqa: E402


def _async_return(value):
    async def _f(*a, **k):
        return value
    return _f


class _FakePos:
    x = 0.001
    y = 0.002


class _FakeStageStub:
    """Stand-in for StageServiceStub that performs no real gRPC."""
    def __init__(self, channel=None, metadata=None):
        pass

    async def get_position(self, req):
        return _FakePos()

    async def move_to(self, req):
        return None


@contextlib.asynccontextmanager
async def _fake_channel(cfg):
    yield (None, None)


def test_move_blocked_when_immersion_active(monkeypatch):
    """Immersion objective active → RuntimeError, and Z is never lowered."""
    calls = {"z": 0}

    async def fake_lower(z):
        calls["z"] += 1

    monkeypatch.setattr(
        oc, "get_current_objective_and_optovar",
        _async_return((stage.IMMERSION_OBJECTIVE_POSITION, 2)),
    )
    monkeypatch.setattr(focus, "move_focus_to_new_z_position", fake_lower)

    with pytest.raises(RuntimeError, match="immersion"):
        asyncio.run(stage.move_stage_to_new_xy_position(0.01, 0.02))

    # The safety property: the guard fires BEFORE the Z=0 lower, so immersion
    # is preserved.
    assert calls["z"] == 0


def test_move_allowed_with_dry_objective(monkeypatch):
    """Dry objective active → no raise, and the normal Z=0 lower runs once."""
    calls = {"z": 0}

    async def fake_lower(z):
        calls["z"] += 1
        assert z == 0   # collision-safety lower to Z=0

    monkeypatch.setattr(
        oc, "get_current_objective_and_optovar",
        _async_return((2, 3)),   # 5x dry objective, not the immersion one
    )
    monkeypatch.setattr(focus, "move_focus_to_new_z_position", fake_lower)
    monkeypatch.setattr(stage, "open_zen_channel", _fake_channel)
    monkeypatch.setattr(stage, "StageServiceStub", _FakeStageStub)
    monkeypatch.setattr(stage, "STAGE_SETTLE_SECONDS", 0.0)

    asyncio.run(stage.move_stage_to_new_xy_position(0.01, 0.02))

    assert calls["z"] == 1
