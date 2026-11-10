# -*- coding: utf-8 -*-
"""Unit tests for the pure validators in MS_CD7_API_LoA.

These pull in the LoA module, which transitively imports zen_api; importorskip
lets the suite run (skipping this module) on a machine without the Zeiss tree.
"""
import pytest

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
pytest.importorskip("zen_api", reason="zen_api not resolvable on this machine")

import MS_CD7_API_LoA as ms  # noqa: E402


@pytest.mark.parametrize("n,ok", [
    (ms.OBJECTIVE_MIN, True),
    (ms.OBJECTIVE_MAX, True),
    (ms.OBJECTIVE_MIN - 1, False),
    (ms.OBJECTIVE_MAX + 1, False),
    (3.0, False),     # float is not int
    ("3", False),     # str is not int
])
def test_validate_objective_number(n, ok):
    valid, msg = ms.validate_objective_number(n)
    assert valid is ok
    assert isinstance(msg, str)


@pytest.mark.parametrize("n,ok", [
    (ms.OPTOVAR_MIN, True),
    (ms.OPTOVAR_MAX, True),
    (ms.OPTOVAR_MIN - 1, False),
    (ms.OPTOVAR_MAX + 1, False),
    (2.0, False),
    ("2", False),
])
def test_validate_optovar_number(n, ok):
    valid, msg = ms.validate_optovar_number(n)
    assert valid is ok
    assert isinstance(msg, str)


@pytest.mark.parametrize("z,ok", [
    (0.0, True),
    (ms.Z_POSITION_MIN, True),
    (ms.Z_POSITION_MAX, True),
    (ms.Z_POSITION_MIN - 0.001, False),
    (ms.Z_POSITION_MAX + 0.001, False),
    ("x", False),
])
def test_validate_z_position(z, ok):
    valid, msg = ms.validate_z_position(z)
    assert valid is ok
    assert isinstance(msg, str)


@pytest.mark.parametrize("x,y,ok", [
    (ms.X_STAGE_MIN, ms.Y_STAGE_MIN, True),
    (ms.X_STAGE_MAX, ms.Y_STAGE_MAX, True),
    (ms.X_STAGE_MAX + 0.01, ms.Y_STAGE_MIN, False),
    (ms.X_STAGE_MIN, ms.Y_STAGE_MAX + 0.01, False),
    (ms.X_STAGE_MIN - 0.01, ms.Y_STAGE_MIN, False),
    ("a", 0.0, False),
    (0.0, "b", False),
])
def test_validate_xy_position(x, y, ok):
    valid, msg = ms.validate_xy_position(x, y)
    assert valid is ok
    assert isinstance(msg, str)


# NaN compares False with everything, so a plain "< lo or > hi" test lets it
# through to the hardware; ±inf and NaN must all be refused.
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_positions_are_refused(bad):
    assert ms.validate_z_position(bad)[0] is False
    assert ms.validate_xy_position(bad, 0.01)[0] is False
    assert ms.validate_xy_position(0.01, bad)[0] is False


# bool is an int in Python: True would otherwise select position 1.
@pytest.mark.parametrize("flag", [True, False])
def test_bool_is_refused(flag):
    assert ms.validate_objective_number(flag)[0] is False
    assert ms.validate_optovar_number(flag)[0] is False
    assert ms.validate_z_position(flag)[0] is False
    assert ms.validate_xy_position(flag, 0.01)[0] is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_moves_raise_before_any_rpc(monkeypatch, bad):
    def no_rpc(coro):
        coro.close()
        raise AssertionError("an RPC was started")

    monkeypatch.setattr(ms.asyncio, "run", no_rpc)
    with pytest.raises(ValueError):
        ms.move_focus_to_new_z_position(bad)
    with pytest.raises(ValueError):
        ms.move_stage_to_new_xy_position(bad, 0.01)


# --------------------------------------------------------------------------
# MS_limits: one module for all layers
# --------------------------------------------------------------------------
def test_the_loa_uses_the_checks_of_ms_limits():
    import MS_limits
    assert ms.validate_z_position is MS_limits.validate_z_position
    assert ms.validate_xy_position is MS_limits.validate_xy_position
    assert (ms.Z_POSITION_MIN, ms.Z_POSITION_MAX) == (MS_limits.Z_POSITION_MIN,
                                                     MS_limits.Z_POSITION_MAX)


@pytest.mark.parametrize("call, args", [
    ("require_z_position", (float("nan"),)),
    ("require_z_position", (1.0,)),
    ("require_xy_position", (0.01, float("inf"))),
    ("require_xy_position", (-1.0, 0.01)),
    ("require_objective_number", (0,)),
    ("require_objective_number", (True,)),
    ("require_optovar_number", (99,)),
])
def test_require_functions_raise(call, args):
    import MS_limits
    with pytest.raises(ValueError, match="Invalid"):
        getattr(MS_limits, call)(*args)


def test_require_functions_pass_a_valid_value():
    import MS_limits
    MS_limits.require_z_position(0)
    MS_limits.require_z_position(-0.0003)
    MS_limits.require_xy_position(0.05, 0.04)
    MS_limits.require_objective_number(MS_limits.OBJECTIVE_MAX)
    MS_limits.require_optovar_number(MS_limits.OPTOVAR_MIN)
