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


def test_move_blocked_when_immersion_active(monkeypatch, fake_objective):
    """Immersion objective active → RuntimeError, and Z is never lowered."""
    calls = {"z": 0}

    async def fake_lower(z):
        calls["z"] += 1

    monkeypatch.setattr(
        oc, "get_active_objective",
        _async_return(fake_objective(stage.IMMERSION_OBJECTIVE_POSITION, immersion=True)),
    )
    monkeypatch.setattr(focus, "move_focus_to_new_z_position", fake_lower)

    with pytest.raises(RuntimeError, match="immersion"):
        asyncio.run(stage.move_stage_to_new_xy_position(0.01, 0.02))

    # The safety property: the guard fires BEFORE the Z=0 lower, so immersion
    # is preserved.
    assert calls["z"] == 0


def test_move_allowed_with_dry_objective(monkeypatch, fake_objective):
    """Dry objective active → no raise, and the normal Z=0 lower runs once."""
    calls = {"z": 0}

    async def fake_lower(z):
        calls["z"] += 1
        assert z == 0   # collision-safety lower to Z=0

    monkeypatch.setattr(
        oc, "get_active_objective",
        _async_return(fake_objective(2)),   # 5x dry objective, not the immersion one
    )
    monkeypatch.setattr(focus, "move_focus_to_new_z_position", fake_lower)
    monkeypatch.setattr(stage, "open_zen_channel", _fake_channel)
    monkeypatch.setattr(stage, "StageServiceStub", _FakeStageStub)
    monkeypatch.setattr(stage, "STAGE_SETTLE_SECONDS", 0.0)

    asyncio.run(stage.move_stage_to_new_xy_position(0.01, 0.02))

    assert calls["z"] == 1


# --------------------------------------------------------------------------
# What counts as "immersion": the API immersion type OR the known slot
# --------------------------------------------------------------------------
def _lowered(monkeypatch):
    calls = {"z": 0}

    async def fake_lower(z):
        calls["z"] += 1

    monkeypatch.setattr(focus, "move_focus_to_new_z_position", fake_lower)
    return calls


def test_move_blocked_for_an_immersion_objective_in_another_slot(monkeypatch, fake_objective):
    """The turret changed: the API says WATER in slot 1. The guard must refuse."""
    calls = _lowered(monkeypatch)
    assert stage.IMMERSION_OBJECTIVE_POSITION != 1
    monkeypatch.setattr(oc, "get_active_objective",
                        _async_return(fake_objective(1, immersion=True, name="40x W")))
    with pytest.raises(stage.ImmersionObjectiveActive, match="40x W"):
        asyncio.run(stage.move_stage_to_new_xy_position(0.01, 0.02))
    assert calls["z"] == 0


def test_move_blocked_in_the_known_slot_even_if_the_api_says_air(monkeypatch, fake_objective):
    """A wrong inventory entry (AIR for the 50x slot) must not switch the guard off."""
    calls = _lowered(monkeypatch)
    monkeypatch.setattr(oc, "get_active_objective",
                        _async_return(fake_objective(stage.IMMERSION_OBJECTIVE_POSITION)))
    with pytest.raises(stage.ImmersionObjectiveActive):
        asyncio.run(stage.move_stage_to_new_xy_position(0.01, 0.02))
    assert calls["z"] == 0


def test_unspecified_immersion_type_counts_as_immersion(fake_objective):
    from zen_api.lm.hardware.v2 import ObjectiveChangerImmersionTypes as T
    obj = fake_objective(1)
    for kind in (T.WATER, T.OIL, T.GLYCERINE, T.SILICONE, T.UNSPECIFIED):
        obj.immersion_type = kind
        assert oc.is_immersion_objective(obj) is True
    obj.immersion_type = T.AIR
    assert oc.is_immersion_objective(obj) is False


def test_immersion_positions_are_api_plus_known_slot(monkeypatch):
    monkeypatch.setattr(oc, "get_immersion_objective_positions", _async_return([1]))
    assert asyncio.run(stage.immersion_objective_positions()) == sorted(
        {1, stage.IMMERSION_OBJECTIVE_POSITION})


# --------------------------------------------------------------------------
# Range check in the lowest layer (MS_limits)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("x, y", [
    (float("nan"), 0.02), (0.01, float("inf")), (5.0, 0.02), (0.01, -0.5), (True, 0.02),
    ("0.01", 0.02),
])
def test_async_xy_move_refuses_a_bad_position(monkeypatch, x, y):
    """Refused before the guard, the Z lower and the stage RPC."""
    calls = _lowered(monkeypatch)

    async def must_not_run():
        raise AssertionError("the objective was read for a refused position")

    monkeypatch.setattr(oc, "get_active_objective", must_not_run)
    with pytest.raises(ValueError, match="XY coordinates"):
        asyncio.run(stage.move_stage_to_new_xy_position(x, y))
    assert calls["z"] == 0


@pytest.mark.parametrize("z", [float("nan"), float("-inf"), 0.5, -300.0, True, None])
def test_async_z_move_refuses_a_bad_position(monkeypatch, z):
    """A µm value given as metres (-300) or NaN never reaches FocusService.MoveTo."""
    def must_not_open(cfg):
        raise AssertionError("a channel was opened for a refused Z")

    monkeypatch.setattr(focus, "open_zen_channel", must_not_open)
    with pytest.raises(ValueError, match="Z position"):
        asyncio.run(focus.move_focus_to_new_z_position(z))
