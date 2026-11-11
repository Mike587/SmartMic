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
    scope.set_optics_verified(hw.focus_obj, hw.focus_opt)
    return scope


def test_df_find_surface(at_focus_optics):
    scope = at_focus_optics
    z = scope.df_find_surface()
    assert scope.Z_POSITION_MIN <= z <= scope.Z_POSITION_MAX


def test_df_repeatability(at_focus_optics, hw):
    scope = at_focus_optics
    z1 = scope.df_find_surface()
    z2 = scope.df_find_surface()
    assert abs(z1 - z2) <= hw.df_repeat_tol_m


def test_df_recall(at_focus_optics):
    scope = at_focus_optics
    # FindSurface with store_focus=True stores the focus; recall should snap back to it.
    z_found = scope.df_find_surface(store_focus=True)
    z_recall = scope.df_recall_focus()
    assert abs(z_recall - z_found) <= 1e-6


def test_swaf_from_path(at_focus_optics, czexp_dir):
    scope = at_focus_optics
    # Get to the surface first so SWAF searches in the right neighbourhood.
    scope.df_find_surface()
    z = scope.swaf_find_focus(czexp_dir / "swaf_coarse_20x.czexp")
    assert scope.Z_POSITION_MIN <= z <= scope.Z_POSITION_MAX
