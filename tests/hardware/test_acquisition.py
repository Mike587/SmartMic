# -*- coding: utf-8 -*-
"""Tier 3 — acquisition from bundled standalone experiment files.

Every experiment is loaded from a repo-owned ``.czexp`` (by path or XML), never
by a name assumed to be in ZEN's library — so the suite is portable to any
machine with the scope. Acquired CZIs are kept for inspection by default
(--clean-images to discard). Skipped unless --run-hardware.
"""
from pathlib import Path

import pytest

import MS_Helper_function as helper

pytestmark = pytest.mark.hardware


def _assert_czi(result):
    """Common checks on a run_experiment_* result dict."""
    assert "experiment_id" in result
    p = result.get("exp_result_path")
    assert p is not None, "no exp_result_path in result"
    p = Path(p)
    assert p.exists(), f"result CZI missing: {p}"
    assert p.stat().st_size > 0
    return p


def test_snap_from_path(scope, czexp_dir, out_dir, run_tag):
    result = scope.run_experiment_from_path(
        czexp_dir / "snap_single.czexp", out_dir, f"test_snap_{run_tag}")
    _assert_czi(result)


def test_zstack_from_path(scope, czexp_dir, out_dir, run_tag):
    result = scope.run_experiment_from_path(
        czexp_dir / "zstack_small.czexp", out_dir, f"test_zstack_{run_tag}")
    p = _assert_czi(result)
    zr = helper.get_zstack_z_range(p)
    if zr is not None:                       # widefield z-stack metadata may vary
        assert zr["n_z"] > 1
        assert zr["step_um"] > 0


def test_from_xml(scope, czexp_dir, out_dir, run_tag):
    # The XML comes from the repo (utf-8-sig drops the BOM; the wrapper also
    # normalizes a BOM / <?xml?> prolog) — fully self-contained.
    xml = (czexp_dir / "snap_single.czexp").read_text(encoding="utf-8-sig")
    result = scope.run_experiment_from_xml(xml, out_dir, f"test_xml_{run_tag}")
    _assert_czi(result)


def test_status_idle_after_acquisition(scope, czexp_dir, out_dir, run_tag):
    scope.run_experiment_from_path(czexp_dir / "snap_single.czexp", out_dir, f"test_idle_{run_tag}")
    status = scope.get_running_experiment_status()
    # Once the synchronous run returns, nothing should be acquiring.
    assert status is None or status["is_acquisition_running"] is False
