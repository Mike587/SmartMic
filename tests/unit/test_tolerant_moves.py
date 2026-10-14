# -*- coding: utf-8 -*-
"""Unit tests for the tolerant Z / XY moves in MS_CD7_API_LoA (tolerance_m).

ZEN sometimes reports a move as failed although the drive is on target
(DEV_NOTES "Spurious 'not reached'"). With tolerance_m the LoA reads the
position back: within tolerance → done; otherwise one retry. The async gRPC
layer is MOCKED, so these run with no microscope.
"""
import pytest

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
pytest.importorskip("zen_api", reason="zen_api not resolvable on this machine")

from grpclib.const import Status                # noqa: E402
from grpclib.exceptions import GRPCError        # noqa: E402

import MS_CD7_API_LoA as ms                     # noqa: E402
import MS_zenapi_focus                          # noqa: E402
import MS_zenapi_stage_LM                        # noqa: E402

Z_TARGET = 1.35e-3
XY_TARGET = (0.0073, 0.0191)


def _not_reached(msg="Requested position was not reached."):
    return GRPCError(Status.INTERNAL, msg)


def _async_seq(outcomes, calls):
    """Async stub: each call pops the next outcome (an exception is raised)."""
    async def _f(*a, **k):
        calls.append(a)
        out = outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out
    return _f


def _async_return(value):
    async def _f(*a, **k):
        return value
    return _f


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(ms.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


# --------------------------------------------------------------------------
# Z
# --------------------------------------------------------------------------
def test_z_without_tolerance_raises_as_before(monkeypatch):
    calls = []
    monkeypatch.setattr(MS_zenapi_focus, "move_focus_to_new_z_position",
                        _async_seq([_not_reached()], calls))
    with pytest.raises(GRPCError):
        ms.move_focus_to_new_z_position(Z_TARGET)
    assert len(calls) == 1


def test_z_error_within_tolerance_is_accepted(monkeypatch):
    calls = []
    monkeypatch.setattr(MS_zenapi_focus, "move_focus_to_new_z_position",
                        _async_seq([_not_reached()], calls))
    # Read-back 0.01 µm off (2026-09-28 B1 attempt 82).
    monkeypatch.setattr(MS_zenapi_focus, "get_current_z_focus_position",
                        _async_return(Z_TARGET + 0.01e-6))
    ms.move_focus_to_new_z_position(Z_TARGET, tolerance_m=0.25e-6)
    assert len(calls) == 1   # accepted, no retry


def test_z_error_off_target_retries_once(monkeypatch):
    calls = []
    monkeypatch.setattr(MS_zenapi_focus, "move_focus_to_new_z_position",
                        _async_seq([_not_reached(), None], calls))
    monkeypatch.setattr(MS_zenapi_focus, "get_current_z_focus_position",
                        _async_return(Z_TARGET + 5e-6))
    ms.move_focus_to_new_z_position(Z_TARGET, tolerance_m=0.25e-6)
    assert len(calls) == 2


def test_z_two_failures_off_target_raise(monkeypatch):
    calls = []
    monkeypatch.setattr(MS_zenapi_focus, "move_focus_to_new_z_position",
                        _async_seq([_not_reached(), _not_reached()], calls))
    monkeypatch.setattr(MS_zenapi_focus, "get_current_z_focus_position",
                        _async_return(Z_TARGET + 5e-6))
    with pytest.raises(GRPCError):
        ms.move_focus_to_new_z_position(Z_TARGET, tolerance_m=0.25e-6)
    assert len(calls) == 2


def test_z_second_failure_within_tolerance_is_accepted(monkeypatch):
    calls = []
    reads = [Z_TARGET + 5e-6, Z_TARGET]   # off after the 1st error, on after the 2nd

    async def read_back():
        return reads.pop(0)

    monkeypatch.setattr(MS_zenapi_focus, "move_focus_to_new_z_position",
                        _async_seq([_not_reached(), _not_reached()], calls))
    monkeypatch.setattr(MS_zenapi_focus, "get_current_z_focus_position", read_back)
    ms.move_focus_to_new_z_position(Z_TARGET, tolerance_m=0.25e-6)
    assert len(calls) == 2


def test_z_unreadable_position_retries_then_raises(monkeypatch):
    calls = []
    monkeypatch.setattr(MS_zenapi_focus, "move_focus_to_new_z_position",
                        _async_seq([_not_reached(), _not_reached()], calls))

    async def read_fails():
        raise _not_reached("gateway gone")

    monkeypatch.setattr(MS_zenapi_focus, "get_current_z_focus_position", read_fails)
    with pytest.raises(GRPCError):
        ms.move_focus_to_new_z_position(Z_TARGET, tolerance_m=0.25e-6)
    assert len(calls) == 2


def test_z_non_grpc_error_is_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(MS_zenapi_focus, "move_focus_to_new_z_position",
                        _async_seq([RuntimeError("something else")], calls))
    with pytest.raises(RuntimeError, match="something else"):
        ms.move_focus_to_new_z_position(Z_TARGET, tolerance_m=0.25e-6)
    assert len(calls) == 1


@pytest.mark.parametrize("bad", [-1e-6, "0.25e-6", True, float("inf"), float("nan")])
def test_z_invalid_tolerance_raises_before_moving(monkeypatch, bad):
    calls = []
    monkeypatch.setattr(MS_zenapi_focus, "move_focus_to_new_z_position",
                        _async_seq([None], calls))
    with pytest.raises(ValueError):
        ms.move_focus_to_new_z_position(Z_TARGET, tolerance_m=bad)
    assert calls == []


# --------------------------------------------------------------------------
# XY
# --------------------------------------------------------------------------
def test_xy_error_within_tolerance_is_accepted_and_settles(monkeypatch, _no_sleep):
    calls = []
    monkeypatch.setattr(MS_zenapi_stage_LM, "move_stage_to_new_xy_position",
                        _async_seq([_not_reached("Position could not be reached.")], calls))
    monkeypatch.setattr(MS_zenapi_stage_LM, "get_current_xy_stage_coordinates",
                        _async_return([XY_TARGET[0] + 0.3e-6, XY_TARGET[1] - 0.3e-6]))
    monkeypatch.setattr(MS_zenapi_stage_LM, "STAGE_SETTLE_SECONDS", 1.5)
    ms.move_stage_to_new_xy_position(*XY_TARGET, tolerance_m=1e-6)
    assert len(calls) == 1
    # The move's own settle was skipped by the error → the LoA settles instead.
    assert _no_sleep == [1.5]


def test_xy_uses_euclidean_distance(monkeypatch):
    calls = []
    monkeypatch.setattr(MS_zenapi_stage_LM, "move_stage_to_new_xy_position",
                        _async_seq([_not_reached(), None], calls))
    # 0.8 µm off on each axis: within 1 µm per axis, but 1.13 µm away → retry.
    monkeypatch.setattr(MS_zenapi_stage_LM, "get_current_xy_stage_coordinates",
                        _async_return([XY_TARGET[0] + 0.8e-6, XY_TARGET[1] + 0.8e-6]))
    ms.move_stage_to_new_xy_position(*XY_TARGET, tolerance_m=1e-6)
    assert len(calls) == 2


def test_xy_two_failures_off_target_raise(monkeypatch):
    calls = []
    monkeypatch.setattr(MS_zenapi_stage_LM, "move_stage_to_new_xy_position",
                        _async_seq([_not_reached(), _not_reached()], calls))
    monkeypatch.setattr(MS_zenapi_stage_LM, "get_current_xy_stage_coordinates",
                        _async_return([XY_TARGET[0] + 5e-6, XY_TARGET[1]]))
    with pytest.raises(GRPCError):
        ms.move_stage_to_new_xy_position(*XY_TARGET, tolerance_m=1e-6)
    assert len(calls) == 2


def test_xy_immersion_refusal_is_never_retried(monkeypatch):
    calls = []
    refusal = MS_zenapi_stage_LM.ImmersionObjectiveActive("immersion objective active")
    monkeypatch.setattr(MS_zenapi_stage_LM, "move_stage_to_new_xy_position",
                        _async_seq([refusal, None], calls))
    with pytest.raises(MS_zenapi_stage_LM.ImmersionObjectiveActive):
        ms.move_stage_to_new_xy_position(*XY_TARGET, tolerance_m=1e-6)
    assert len(calls) == 1


def test_xy_without_tolerance_raises_as_before(monkeypatch):
    calls = []
    monkeypatch.setattr(MS_zenapi_stage_LM, "move_stage_to_new_xy_position",
                        _async_seq([_not_reached()], calls))
    with pytest.raises(GRPCError):
        ms.move_stage_to_new_xy_position(*XY_TARGET)
    assert len(calls) == 1
