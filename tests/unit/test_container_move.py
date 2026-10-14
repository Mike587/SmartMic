# -*- coding: utf-8 -*-
"""Unit tests for the safe container (slide) move.

MS_zenapi_sample_carrier.move_to_container must behave like the direct XY move:
refuse with the immersion objective active (and then move NOTHING, not even Z),
otherwise lower Z to 0 BEFORE MoveToContainer. The LoA wrapper validates the
indices and resets a DefiniteFocusTracker. The gRPC layer is MOCKED.
"""
import asyncio
import contextlib

import pytest

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
pytest.importorskip("zen_api", reason="zen_api not resolvable on this machine")

import MS_CD7_API_LoA as ms                    # noqa: E402
import MS_zenapi_focus as focus                # noqa: E402
import MS_zenapi_objectivechanger as oc        # noqa: E402
import MS_zenapi_sample_carrier as sc          # noqa: E402
import MS_zenapi_stage_LM as stage             # noqa: E402


def _async_return(value):
    async def _f(*a, **k):
        return value
    return _f


@contextlib.asynccontextmanager
async def _fake_channel(cfg):
    yield (None, None)


@pytest.fixture
def events(monkeypatch):
    """Record the order of Z lowers and container moves."""
    ev = []

    async def fake_lower(z):
        ev.append(("z", z))

    class _FakeCarrierStub:
        def __init__(self, channel=None, metadata=None):
            pass

        async def move_to_container(self, req):
            ev.append(("container", req.row_index, req.column_index))

    monkeypatch.setattr(focus, "move_focus_to_new_z_position", fake_lower)
    monkeypatch.setattr(sc, "open_zen_channel", _fake_channel)
    monkeypatch.setattr(sc, "SampleCarrierServiceStub", _FakeCarrierStub)
    monkeypatch.setattr(stage, "STAGE_SETTLE_SECONDS", 0.0)
    return ev


def test_container_move_refused_with_immersion(monkeypatch, events):
    monkeypatch.setattr(oc, "get_current_objective_and_optovar",
                        _async_return((stage.IMMERSION_OBJECTIVE_POSITION, 2)))
    with pytest.raises(stage.ImmersionObjectiveActive, match="immersion"):
        asyncio.run(sc.move_to_container(0, 1))
    assert events == []   # neither Z nor the stage moved


def test_container_move_lowers_z_first(monkeypatch, events):
    monkeypatch.setattr(oc, "get_current_objective_and_optovar", _async_return((3, 1)))
    asyncio.run(sc.move_to_container(0, 2))
    assert events == [("z", 0), ("container", 0, 2)]


def test_immersion_refusal_is_a_runtime_error():
    # Existing callers catch RuntimeError; the dedicated type must stay one.
    assert issubclass(stage.ImmersionObjectiveActive, RuntimeError)


# --------------------------------------------------------------------------
# LoA wrapper
# --------------------------------------------------------------------------
@pytest.mark.parametrize("row,col", [(-1, 0), (0, -1), (0.0, 1), ("0", 1), (True, 0)])
def test_loa_move_to_container_invalid_index_raises_before_moving(monkeypatch, row, col):
    calls = []

    async def fake_move(r, c):
        calls.append((r, c))

    monkeypatch.setattr(sc, "move_to_container", fake_move)
    with pytest.raises(ValueError):
        ms.move_to_container(row, col)
    assert calls == []


def test_loa_move_to_container_resets_tracker(monkeypatch):
    calls = []

    async def fake_move(r, c):
        calls.append((r, c))

    monkeypatch.setattr(sc, "move_to_container", fake_move)
    tracker = ms.DefiniteFocusTracker(surface_z_m=1.35e-3)
    tracker.consecutive_failures = 2
    ms.move_to_container(0, 1, focus_tracker=tracker)
    assert calls == [(0, 1)]
    assert tracker.last_surface_z_m is None
    assert tracker.consecutive_failures == 2   # a slide change doesn't fix a DF fault


def test_loa_move_to_container_resets_tracker_even_if_refused(monkeypatch):
    async def refused(r, c):
        raise stage.ImmersionObjectiveActive("immersion")

    monkeypatch.setattr(sc, "move_to_container", refused)
    tracker = ms.DefiniteFocusTracker(surface_z_m=1.35e-3)
    with pytest.raises(stage.ImmersionObjectiveActive):
        ms.move_to_container(0, 1, focus_tracker=tracker)
    assert tracker.last_surface_z_m is None
