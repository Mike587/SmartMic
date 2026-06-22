# -*- coding: utf-8 -*-
"""Tier 1 — controlled motion: stage, Z, objective/optovar.

Each test that moves is wrapped by ``restore_state`` so a failure can't poison
the next. Stage targets come from the bundled positions file (valid wells for
the loaded plate). Skipped unless --run-hardware.
"""
import pytest

import MS_Helper_function as helper

pytestmark = pytest.mark.hardware


@pytest.fixture
def first_position(czexp_dir):
    """First used plate position (x_m, y_m) from the bundled positions file."""
    positions = helper.load_positions_from_czexp(czexp_dir / "positions_384.czexp")
    if not positions:
        pytest.skip("no usable positions in positions_384.czexp")
    p = positions[0]
    return p["x_m"], p["y_m"]


def test_stage_round_trip(restore_state, hw, first_position):
    scope = restore_state
    x0, y0 = scope.get_current_xy_stage_position()
    tx, ty = first_position
    scope.move_stage_to_new_xy_position(tx, ty)
    x1, y1 = scope.get_current_xy_stage_position()
    assert abs(x1 - tx) <= hw.stage_tol_m
    assert abs(y1 - ty) <= hw.stage_tol_m
    # restore_state returns to (x0, y0) on teardown.


def test_z_round_trip(restore_state, hw):
    scope = restore_state
    z0 = scope.get_current_z_position()
    target = z0 + 5e-6   # +5 µm, well within the ±0.01 m safe range
    assert scope.Z_POSITION_MIN <= target <= scope.Z_POSITION_MAX
    scope.move_focus_to_new_z_position(target)
    z1 = scope.get_current_z_position()
    assert abs(z1 - target) <= hw.z_tol_m


def test_objective_optovar_change(restore_state, hw):
    scope = restore_state
    ok = scope.set_objective_set_optovar_sync(hw.safe_obj, hw.safe_opt)
    assert ok is True
    assert scope.get_current_objective_and_optovar() == (hw.safe_obj, hw.safe_opt)


# --- guard checks: invalid input must raise before any motion ---
def test_invalid_z_raises(scope):
    with pytest.raises(ValueError):
        scope.move_focus_to_new_z_position(1.0)   # 1 m — far outside ±0.01


def test_invalid_xy_raises(scope):
    with pytest.raises(ValueError):
        scope.move_stage_to_new_xy_position(99.0, 99.0)


def test_invalid_objective_raises(scope):
    with pytest.raises(ValueError):
        scope.set_objective_set_optovar_sync(99, 1)
