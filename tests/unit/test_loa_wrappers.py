# -*- coding: utf-8 -*-
"""Unit tests for the LoA sync wrappers with the async gRPC layer MOCKED.

These exercise the orchestration logic (retry/exception handling, routing,
validate-before-call) without a microscope by monkeypatching the underlying
MS_zenapi_* async functions.
"""
import pytest

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
pytest.importorskip("zen_api", reason="zen_api not resolvable on this machine")

import MS_CD7_API_LoA as ms                 # noqa: E402
import MS_zenapi_focus                       # noqa: E402
import MS_zenapi_experiment_methods          # noqa: E402
import MS_zenapi_objectivechanger            # noqa: E402
import MS_zenapi_stage_LM                     # noqa: E402


def _async_return(value):
    async def _f(*a, **k):
        return value
    return _f


def _async_raise(exc):
    async def _f(*a, **k):
        raise exc
    return _f


# --------------------------------------------------------------------------
# run_definite_focus_find_surface
# --------------------------------------------------------------------------
def test_df_find_surface_success(monkeypatch):
    monkeypatch.setattr(MS_zenapi_focus, "definite_focus_find_surface", _async_return((True, 2)))
    ok, msg, attempts = ms.run_definite_focus_find_surface()
    assert ok is True
    assert attempts == 2
    assert isinstance(msg, str)


def test_df_find_surface_failure(monkeypatch):
    monkeypatch.setattr(MS_zenapi_focus, "definite_focus_find_surface", _async_return((False, 3)))
    ok, msg, attempts = ms.run_definite_focus_find_surface()
    assert ok is False
    assert attempts == 3


def test_df_find_surface_exception_caught(monkeypatch):
    monkeypatch.setattr(MS_zenapi_focus, "definite_focus_find_surface", _async_raise(RuntimeError("boom")))
    ok, msg, attempts = ms.run_definite_focus_find_surface(max_retries=4)
    assert ok is False
    assert "boom" in msg
    assert attempts == 4   # exception path reports max_retries


# --------------------------------------------------------------------------
# run_experiment routing
# --------------------------------------------------------------------------
def test_run_experiment_routes_by_name(monkeypatch):
    calls = {}

    async def by_name(**k):
        calls["by_name"] = k
        return {"exp_result_path": "x"}

    async def check(**k):
        calls["check"] = k
        return {}

    monkeypatch.setattr(MS_zenapi_experiment_methods, "run_experiment_by_name", by_name)
    monkeypatch.setattr(MS_zenapi_experiment_methods, "check_experiment_api", check)

    res = ms.run_experiment("EXP", custom_folder=None, custom_filename="f", do_snap_and_live=False)
    assert "by_name" in calls and "check" not in calls
    assert res == {"exp_result_path": "x"}


def test_run_experiment_routes_snap_and_live(monkeypatch):
    calls = {}

    async def by_name(**k):
        calls["by_name"] = k
        return {}

    async def check(**k):
        calls["check"] = k
        return {"snap_path": None}

    monkeypatch.setattr(MS_zenapi_experiment_methods, "run_experiment_by_name", by_name)
    monkeypatch.setattr(MS_zenapi_experiment_methods, "check_experiment_api", check)

    ms.run_experiment("EXP", do_snap_and_live=True)
    assert "check" in calls and "by_name" not in calls


def test_run_experiment_empty_name_raises():
    with pytest.raises(ValueError):
        ms.run_experiment("")


# --------------------------------------------------------------------------
# validate-before-async: invalid input must raise before any gRPC call
# --------------------------------------------------------------------------
def test_move_focus_invalid_raises_before_async(monkeypatch):
    called = {"n": 0}

    async def stub(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(MS_zenapi_focus, "move_focus_to_new_z_position", stub)
    with pytest.raises(ValueError):
        ms.move_focus_to_new_z_position(1.0)   # 1 m — far outside ±0.01
    assert called["n"] == 0


def test_move_stage_invalid_raises_before_async(monkeypatch):
    called = {"n": 0}

    async def stub(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(MS_zenapi_stage_LM, "move_stage_to_new_xy_position", stub)
    with pytest.raises(ValueError):
        ms.move_stage_to_new_xy_position(99.0, 99.0)
    assert called["n"] == 0


# --------------------------------------------------------------------------
# set_objective_set_optovar_sync
# --------------------------------------------------------------------------
def test_set_optics_invalid_objective_raises():
    with pytest.raises(ValueError):
        ms.set_objective_set_optovar_sync(99, 1)


def test_set_optics_invalid_optovar_raises():
    with pytest.raises(ValueError):
        ms.set_objective_set_optovar_sync(2, 99)


def test_set_optics_happy_path(monkeypatch):
    async def get_current():
        return (2, 2)

    async def set_oo(obj, opt):
        return None

    monkeypatch.setattr(MS_zenapi_objectivechanger, "get_current_objective_and_optovar", get_current)
    monkeypatch.setattr(MS_zenapi_objectivechanger, "set_objective_set_optovar", set_oo)
    # Target already matches what get_current reports → confirms on first poll.
    assert ms.set_objective_set_optovar_sync(2, 2) is True
