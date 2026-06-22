# -*- coding: utf-8 -*-
"""Tier 2 — focus search: DefiniteFocus (find/recall) and SWAF.

The bundled SWAF experiments are tuned for the 20×/0.95 objective, so the
``at_focus_optics`` fixture sets FOCUS_OBJ/FOCUS_OPT first.

DefiniteFocus FindSurface is run with the library default start (−300 µm): DF is
designed to locate the surface from any starting position, so a fixed safe start
is fine. (Surface Z is plate-dependent — notably on the carrier's *skirt* — which
is exactly why the test does not assume a particular surface Z. The library now
tolerates a start position the drive can't fully reach; see TEST_PLAN §12.)

Assertions check success + plausible (in-range) values and DF repeatability —
never hard-coded µm. Skipped unless --run-hardware.
"""
import pytest

pytestmark = pytest.mark.hardware


@pytest.fixture
def at_focus_optics(restore_state, hw):
    """Set the 20×/0.95 focus optics, yield the scope; restore on teardown."""
    scope = restore_state
    assert scope.set_objective_set_optovar_sync(hw.focus_obj, hw.focus_opt) is True
    return scope


def test_df_find_surface(at_focus_optics):
    scope = at_focus_optics
    ok, msg, attempts = scope.run_definite_focus_find_surface()
    assert ok is True, msg
    assert attempts >= 1
    z = scope.get_current_z_position()
    assert scope.Z_POSITION_MIN <= z <= scope.Z_POSITION_MAX


def test_df_repeatability(at_focus_optics, hw):
    scope = at_focus_optics
    ok1, msg1, _ = scope.run_definite_focus_find_surface()
    assert ok1 is True, msg1
    z1 = scope.get_current_z_position()
    ok2, msg2, _ = scope.run_definite_focus_find_surface()
    assert ok2 is True, msg2
    z2 = scope.get_current_z_position()
    assert abs(z1 - z2) <= hw.df_repeat_tol_m


def test_df_recall(at_focus_optics):
    scope = at_focus_optics
    # FindSurface stores a focus position; recall should snap back to it.
    ok, msg, _ = scope.run_definite_focus_find_surface()
    assert ok is True, msg
    z_um, attempts = scope.run_definite_focus_recall()
    assert z_um is not None
    assert isinstance(z_um, float)
    assert attempts >= 1


def test_swaf_from_path(at_focus_optics, czexp_dir):
    scope = at_focus_optics
    # Get to the surface first so SWAF searches in the right neighbourhood.
    ok, msg, _ = scope.run_definite_focus_find_surface()
    assert ok is True, msg
    focus_um, attempts = scope.run_swaf_from_path(czexp_dir / "swaf_coarse_20x.czexp")
    assert attempts >= 1
    assert focus_um is not None, "SWAF returned no focus position"
    assert isinstance(focus_um, float)
