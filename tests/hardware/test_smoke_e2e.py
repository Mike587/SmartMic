# -*- coding: utf-8 -*-
"""Tier 4 — end-to-end smoke test: the full acquisition chain at one position.

This is the "overall test": move → optics → DefiniteFocus → SWAF (coarse+fine,
from path) → acquire overview → acquire z-stack, asserting each stage produces
its artifact. It proves the LoA wrappers compose correctly against the real
scope. The external nuclei analysis is intentionally NOT run here (it lives in a
separate pixi env and is non-deterministic); this test targets one known plate
position directly. Skipped unless --run-hardware.
"""
from pathlib import Path

import pytest

import MS_Helper_function as helper

pytestmark = [pytest.mark.hardware, pytest.mark.slow]


def test_full_chain_one_position(restore_state, hw, czexp_dir, out_dir, run_tag):
    scope = restore_state

    # 1. Move to the first known plate position.
    positions = helper.load_positions_from_czexp(czexp_dir / "positions_384.czexp")
    if not positions:
        pytest.skip("no usable positions in positions_384.czexp")
    p0 = positions[0]
    scope.move_stage_to_new_xy_position(p0["x_m"], p0["y_m"])

    # 2. Set the focus optics (20×/0.95) the SWAF experiments are tuned for.
    assert scope.set_objective_set_optovar_sync(hw.focus_obj, hw.focus_opt) is True

    # 3. DefiniteFocus FindSurface.
    ok, msg, _ = scope.run_definite_focus_find_surface()
    assert ok is True, msg

    # 4. SWAF coarse + fine, both from bundled paths.
    coarse_um, _ = scope.run_swaf_from_path(czexp_dir / "swaf_coarse_20x.czexp")
    assert coarse_um is not None, "SWAF coarse failed"
    fine_um, _ = scope.run_swaf_from_path(czexp_dir / "swaf_fine_20x.czexp")
    assert fine_um is not None, "SWAF fine failed"

    # 5. Acquire an overview image.
    ov = scope.run_experiment_from_path(czexp_dir / "snap_single.czexp", out_dir, f"e2e_overview_{run_tag}")
    assert Path(ov["exp_result_path"]).exists()

    # 6. Acquire a confocal z-stack.
    zs = scope.run_experiment_from_path(czexp_dir / "zstack_small.czexp", out_dir, f"e2e_zstack_{run_tag}")
    assert Path(zs["exp_result_path"]).exists()
