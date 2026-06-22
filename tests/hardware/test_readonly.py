# -*- coding: utf-8 -*-
"""Tier 0 — read-only hardware checks. Moves nothing.

Pytest-ified equivalent of verify_zen_api.py: confirms the gRPC API answers and
the reported state is well-formed and in range. Safe to run any time the scope
is up. Skipped unless --run-hardware (and the expected carrier is loaded).
"""
import pytest

pytestmark = pytest.mark.hardware


def test_sample_carrier_name(scope, hw):
    assert scope.get_sample_carrier_name() == hw.carrier


def test_sample_carrier_info_shape(scope, hw):
    info = scope.get_sample_carrier_info()
    assert isinstance(info, dict)
    assert info.get("name") == hw.carrier
    # Documented keys (MS_zenapi_sample_carrier.get_sample_carrier_info).
    for key in ("name", "rows", "columns", "material", "thickness",
                "skirt", "refractive_index"):
        assert key in info


def test_current_objective_and_optovar_in_range(scope):
    obj, opt = scope.get_current_objective_and_optovar()
    assert scope.OBJECTIVE_MIN <= obj <= scope.OBJECTIVE_MAX
    assert scope.OPTOVAR_MIN <= opt <= scope.OPTOVAR_MAX


def test_current_optics_names(scope):
    (obj_name, obj_pos), (opt_name, opt_pos) = scope.get_current_objective_and_optovar_names()
    assert isinstance(obj_name, str) and obj_name
    assert isinstance(opt_name, str) and opt_name
    # Positions must agree with the index-only query.
    assert (obj_pos, opt_pos) == scope.get_current_objective_and_optovar()


def test_xy_position_in_range(scope):
    xy = scope.get_current_xy_stage_position()
    assert len(xy) == 2
    x, y = xy
    assert scope.X_STAGE_MIN <= x <= scope.X_STAGE_MAX
    assert scope.Y_STAGE_MIN <= y <= scope.Y_STAGE_MAX


def test_z_position_in_range(scope):
    z = scope.get_current_z_position()
    assert isinstance(z, float)
    assert scope.Z_POSITION_MIN <= z <= scope.Z_POSITION_MAX


def test_is_microscope_busy_is_bool(scope):
    assert isinstance(scope.is_microscope_busy(), bool)


def test_running_experiment_status(scope):
    status = scope.get_running_experiment_status()
    assert status is None or isinstance(status, dict)
    if isinstance(status, dict):
        for key in ("is_experiment_running", "is_acquisition_running",
                    "images_acquired_index", "images_count",
                    "scenes_index", "scenes_count"):
            assert key in status
