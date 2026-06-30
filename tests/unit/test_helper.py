# -*- coding: utf-8 -*-
"""Unit tests for MS_Helper_function — logging, position loading, focus scoring.

The focus-scoring tests read the bundled CZIs (need pylibCZIrw, present in the
smartmic env); they importorskip so the suite still runs where it is absent.
"""
import logging

import pytest

import MS_Helper_function as helper


# --------------------------------------------------------------------------
# load_positions_from_czexp
# --------------------------------------------------------------------------
@pytest.fixture
def positions_path(czexp_dir):
    return czexp_dir / "positions_384.czexp"


def test_load_positions_shape(positions_path):
    positions = helper.load_positions_from_czexp(positions_path)
    assert isinstance(positions, list)
    assert len(positions) > 0
    for p in positions:
        assert {"well", "position_name", "scene_index", "x_m", "y_m", "z_m"} <= set(p)
        assert isinstance(p["scene_index"], int)
        for k in ("x_m", "y_m", "z_m"):
            assert isinstance(p[k], float)
        # µm → m conversion ⇒ stage coords are sub-metre.
        assert abs(p["x_m"]) < 1.0 and abs(p["y_m"]) < 1.0


def test_load_positions_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        helper.load_positions_from_czexp(tmp_path / "nope.czexp")


# P1 has no flag (→ used by default); P2 is explicitly false (→ excluded);
# the whole B2 array is false (→ all its positions excluded).
POS_XML = """<HardwareExperiment>
  <SingleTileRegionArray Name="A1">
    <SingleTileRegions>
      <SingleTileRegion Name="P1"><X>1000</X><Y>2000</Y><Z>3000</Z></SingleTileRegion>
      <SingleTileRegion Name="P2"><X>1</X><Y>2</Y><Z>3</Z><IsUsedForAcquisition>false</IsUsedForAcquisition></SingleTileRegion>
    </SingleTileRegions>
  </SingleTileRegionArray>
  <SingleTileRegionArray Name="B2">
    <IsUsedForAcquisition>false</IsUsedForAcquisition>
    <SingleTileRegions>
      <SingleTileRegion Name="P1"><X>9</X><Y>9</Y><Z>9</Z></SingleTileRegion>
    </SingleTileRegions>
  </SingleTileRegionArray>
</HardwareExperiment>"""


def test_load_positions_used_flag_defaults_true(tmp_path):
    f = tmp_path / "synthetic.czexp"
    f.write_text(POS_XML, encoding="utf-8")
    positions = helper.load_positions_from_czexp(f)
    assert len(positions) == 1
    p = positions[0]
    assert p["well"] == "A1"
    assert p["position_name"] == "P1"
    assert p["x_m"] == pytest.approx(1000 / 1e6)


# --------------------------------------------------------------------------
# setup_run_logger
# --------------------------------------------------------------------------
def test_setup_run_logger(tmp_path):
    logger, log_file = helper.setup_run_logger(tmp_path, name="smartmic_test")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "smartmic_test"
    assert log_file.exists()
    n = len(logger.handlers)
    logger2, _ = helper.setup_run_logger(tmp_path, name="smartmic_test")
    assert logger2 is logger
    assert len(logger2.handlers) == n   # handlers.clear() prevents accumulation


# --------------------------------------------------------------------------
# Focus scoring / CZI metadata (need pylibCZIrw + the bundled CZIs)
# --------------------------------------------------------------------------
@pytest.fixture
def sharp(czi_dir):
    return czi_dir / "sharp_small.czi"


@pytest.fixture
def blurry(czi_dir):
    return czi_dir / "blurry_small.czi"


@pytest.fixture
def zstack_czi(czi_dir):
    return czi_dir / "zstack.czi"


def test_compute_focus_score_sharp_gt_blurry(sharp, blurry):
    pytest.importorskip("pylibCZIrw")
    s = helper.compute_focus_score(sharp)
    b = helper.compute_focus_score(blurry)
    assert s is not None and b is not None
    assert s > b


def test_compute_focus_score_bad_path_returns_none(tmp_path):
    assert helper.compute_focus_score(tmp_path / "nope.czi") is None


def test_get_focus_position_from_czi(zstack_czi):
    fp = helper.get_focus_position_from_czi(zstack_czi)
    assert fp is None or isinstance(fp, float)


def test_get_focus_position_missing_returns_none(tmp_path):
    assert helper.get_focus_position_from_czi(tmp_path / "nope.czi") is None


def test_get_zstack_z_range(zstack_czi):
    pytest.importorskip("pylibCZIrw")
    zr = helper.get_zstack_z_range(zstack_czi)
    assert zr is not None
    assert set(zr) == {"first_um", "center_um", "last_um", "n_z", "step_um"}
    assert zr["n_z"] > 1
    assert zr["step_um"] > 0
    assert zr["last_um"] == pytest.approx(zr["first_um"] + (zr["n_z"] - 1) * zr["step_um"])


def test_get_zstack_z_range_single_plane_is_none(sharp):
    pytest.importorskip("pylibCZIrw")
    assert helper.get_zstack_z_range(sharp) is None


# --------------------------------------------------------------------------
# Stage position from CZI (get_stage_position_from_czi / get_xy_position_from_czi)
# --------------------------------------------------------------------------
def test_get_stage_position_from_czi_shape(zstack_czi):
    pytest.importorskip("pylibCZIrw")
    info = helper.get_stage_position_from_czi(zstack_czi)
    assert info is not None
    assert set(info) == {"actual_x_m", "actual_y_m",
                         "planned_x_m", "planned_y_m", "z_m"}
    # All five fields are present in this fixture; values are plausible stage
    # coordinates in metres (this scope's travel is well under 0.2 m).
    for k, v in info.items():
        assert isinstance(v, float)
        assert abs(v) < 0.2


def test_get_stage_position_from_czi_missing_returns_none(tmp_path):
    assert helper.get_stage_position_from_czi(tmp_path / "nope.czi") is None


def test_get_xy_position_prefers_planned_vs_actual(zstack_czi):
    pytest.importorskip("pylibCZIrw")
    info = helper.get_stage_position_from_czi(zstack_czi)
    # planned (scene CenterPosition) is the reliable "where imaged" field and the
    # default; actual (MTBStageAxis encoder) is the parked position. In this
    # fixture the two differ, so each prefer-mode must return its own pair.
    assert helper.get_xy_position_from_czi(zstack_czi) == (
        info["planned_x_m"], info["planned_y_m"])
    assert helper.get_xy_position_from_czi(zstack_czi, prefer="planned") == (
        info["planned_x_m"], info["planned_y_m"])
    assert helper.get_xy_position_from_czi(zstack_czi, prefer="actual") == (
        info["actual_x_m"], info["actual_y_m"])


def test_get_xy_position_from_czi_missing_returns_none(tmp_path):
    assert helper.get_xy_position_from_czi(tmp_path / "nope.czi") is None


# --------------------------------------------------------------------------
# Signal level / empty-stack guard
# --------------------------------------------------------------------------
def test_signal_level_from_czi_real_image(sharp):
    pytest.importorskip("pylibCZIrw")
    level = helper.signal_level_from_czi(sharp)
    # A real (non-empty) image: signal well above the empty threshold, ≤ 1.
    assert level is not None
    assert helper.EMPTY_STACK_SIGNAL_FRAC < level <= 1.0


def test_signal_level_from_czi_missing_returns_none(tmp_path):
    assert helper.signal_level_from_czi(tmp_path / "nope.czi") is None


def test_is_czi_effectively_empty_false_for_real_image(sharp):
    pytest.importorskip("pylibCZIrw")
    # Default threshold: a real image is not empty.
    assert helper.is_czi_effectively_empty(sharp) is False


def test_is_czi_effectively_empty_true_above_signal(sharp):
    pytest.importorskip("pylibCZIrw")
    # Force the empty branch with a frac above this image's signal level
    # (no all-black fixture available) — exercises the comparison.
    assert helper.is_czi_effectively_empty(sharp, frac=1.0) is True


def test_is_czi_effectively_empty_missing_returns_none(tmp_path):
    assert helper.is_czi_effectively_empty(tmp_path / "nope.czi") is None
