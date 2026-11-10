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


def _positions_file(tmp_path, region_xml):
    f = tmp_path / "synthetic.czexp"
    f.write_text(f'<HardwareExperiment><SingleTileRegionArray Name="C7">'
                 f'<SingleTileRegions>{region_xml}</SingleTileRegions>'
                 f'</SingleTileRegionArray></HardwareExperiment>', encoding="utf-8")
    return f


# A coordinate that cannot be read must stop the run: a silent 0.0 would send
# the stage to (0, 0).
@pytest.mark.parametrize("coords", [
    "<Y>2</Y><Z>3</Z>",                      # X missing
    "<X>1</X><Z>3</Z>",                      # Y missing
    "<X></X><Y>2</Y><Z>3</Z>",               # X empty
    "<X>abc</X><Y>2</Y><Z>3</Z>",            # X not a number
    "<X>1</X><Y>NaN</Y><Z>3</Z>",            # Y not finite
    "<X>1</X><Y>2</Y><Z>inf</Z>",            # Z not finite
    "<X>1</X><Y>2</Y><Z>1,5</Z>",            # Z not a number
])
def test_load_positions_bad_coordinate_raises(tmp_path, coords):
    f = _positions_file(tmp_path, f'<SingleTileRegion Name="P3">{coords}</SingleTileRegion>')
    with pytest.raises(ValueError, match="C7.*P3"):
        helper.load_positions_from_czexp(f)


def test_load_positions_missing_z_is_zero(tmp_path):
    f = _positions_file(tmp_path,
                        '<SingleTileRegion Name="P1"><X>1</X><Y>2</Y></SingleTileRegion>')
    assert helper.load_positions_from_czexp(f)[0]["z_m"] == 0.0


def test_load_positions_unused_position_is_not_checked(tmp_path):
    f = _positions_file(
        tmp_path,
        '<SingleTileRegion Name="P1"><X>1</X><Y>2</Y><Z>3</Z></SingleTileRegion>'
        '<SingleTileRegion Name="P2"><X>abc</X>'
        '<IsUsedForAcquisition>false</IsUsedForAcquisition></SingleTileRegion>')
    assert [p["position_name"] for p in helper.load_positions_from_czexp(f)] == ["P1"]


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
# write_run_info (version record next to the run log)
# --------------------------------------------------------------------------
@pytest.fixture
def fake_versions(monkeypatch):
    import zeiss_paths as zp
    monkeypatch.setattr("MS_zen_logs.zen_app_version", lambda: "3.13.1")
    monkeypatch.setattr(zp, "zen_api_gateway_version", lambda: "1.2.3.4")
    monkeypatch.setattr(zp, "zen_api_version", lambda: ("2025.10.1", "C:/zen_api"))
    monkeypatch.setattr(zp, "smartmic_version", lambda: "abc1234-dirty")


def test_write_run_info_record(tmp_path, git_repo, fake_versions, caplog):
    import json
    repo, commit = git_repo
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    log_file = tmp_path / "logs" / "run_20261109_120000.log"
    log_file.parent.mkdir()
    log = logging.getLogger("smartmic_test_run_info")

    with caplog.at_level(logging.INFO, logger=log.name):
        path = helper.write_run_info(log_file, log,
                                     repos={"project": repo, "analysis": tmp_path / "not_a_repo"},
                                     extra={"plate": 4}, script=tmp_path / "MS_x.py")

    assert path == log_file.with_name("run_info_20261109_120000.json")
    info = json.loads(path.read_text(encoding="utf-8"))
    assert info["script"] == str(tmp_path / "MS_x.py")
    assert info["smartmic"]["describe"] == "abc1234-dirty"
    assert len(info["smartmic"]["commit"]) == 40           # SmartMic's own checkout
    assert info["repos"]["project"] == {"path": str(repo), "root": str(repo), "commit": commit,
                                        "dirty_files": ["tracked.txt"]}
    assert info["repos"]["analysis"]["commit"] is None
    assert (info["zen_app"], info["zen_api_gateway"]) == ("3.13.1", "1.2.3.4")
    assert info["zen_api"] == {"version": "2025.10.1", "path": "C:/zen_api"}
    assert info["extra"] == {"plate": 4}

    # Uncommitted code in a run is a WARNING naming the files; a missing repo says so.
    warn = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(m.startswith(f"project commit {commit}") and "tracked.txt" in m for m in warn)
    assert any("analysis commit unknown" in r.getMessage() for r in caplog.records)
    assert any(str(path) in r.getMessage() for r in caplog.records)


def test_write_run_info_other_log_name(tmp_path, fake_versions):
    path = helper.write_run_info(tmp_path / "probe.log", logging.getLogger("smartmic_test_run_info"))
    assert path.name == "probe_run_info.json" and path.exists()


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


# --------------------------------------------------------------------------
# append_jsonl / read_jsonl (resumable run records)
# --------------------------------------------------------------------------
def test_jsonl_roundtrip(tmp_path):
    log = tmp_path / "sub" / "cells.jsonl"           # folder created on demand
    helper.append_jsonl(log, {"cell": 1, "status": "ok", "z_m": 1.35e-3})
    helper.append_jsonl(log, {"cell": 2, "status": "no_midplane", "note": "µ"})
    assert helper.read_jsonl(log) == [
        {"cell": 1, "status": "ok", "z_m": 1.35e-3},
        {"cell": 2, "status": "no_midplane", "note": "µ"}]


def test_read_jsonl_missing_file_is_empty(tmp_path):
    assert helper.read_jsonl(tmp_path / "nope.jsonl") == []


def test_jsonl_line_cut_off_by_a_crash(tmp_path):
    log = tmp_path / "cells.jsonl"
    helper.append_jsonl(log, {"cell": 1})
    with open(log, "a", encoding="utf-8") as f:     # crash mid-write: no newline
        f.write('{"cell": 2, "sta')
    assert helper.read_jsonl(log) == [{"cell": 1}]
    # The next record must not be glued onto the fragment.
    helper.append_jsonl(log, {"cell": 2})
    assert helper.read_jsonl(log) == [{"cell": 1}, {"cell": 2}]


def test_read_jsonl_skips_blank_lines(tmp_path):
    log = tmp_path / "cells.jsonl"
    log.write_text('{"a": 1}\n\n{"b": 2}\n', encoding="utf-8")
    assert helper.read_jsonl(log) == [{"a": 1}, {"b": 2}]


# --------------------------------------------------------------------------
# Sample carrier (SampleHolder Template) from .czi / .czexp
# --------------------------------------------------------------------------
def test_sample_carrier_from_czi_plate(sharp):
    pytest.importorskip("pylibCZIrw")
    c = helper.get_sample_carrier_from_czi(sharp)
    assert c["name"] == "Multichamber 384"
    assert c["category"] == "Multichamber"
    assert (c["rows"], c["columns"]) == (16, 24)
    # µm in the file → m: 4.5 mm pitch, SBS footprint 127.8 × 85.5 mm.
    assert c["pitch_x_m"] == pytest.approx(4.5e-3)
    assert c["pitch_y_m"] == pytest.approx(4.5e-3)
    assert c["bounds_size_m"] == pytest.approx((0.1278, 0.0855))
    assert c["insert_type"] == "Default"
    assert c["is_part_of_insert"] is False


def test_sample_carrier_from_czexp_96well(czexp_dir):
    c = helper.get_sample_carrier_from_czexp(czexp_dir / "zstack_LSM.czexp")
    assert c["name"] == "Multiwell 96"
    assert (c["rows"], c["columns"]) == (8, 12)
    assert c["pitch_x_m"] == pytest.approx(9e-3)
    assert c["is_part_of_insert"] is False


def test_sample_carrier_ignores_czsht_copy(czexp_dir, caplog):
    # stitch_region also holds a "Multichamber 384.czsht" copy under
    # ScanAreaSampleHolderInfo, and nested SampleHolders without a Template.
    with caplog.at_level(logging.WARNING, logger="smartmic"):
        c = helper.get_sample_carrier_from_czexp(czexp_dir / "stitch_region.czexp")
    assert c["name"] == "Multichamber 384"
    assert "more than one sample carrier" not in caplog.text


# Trimmed from a Marc_SM_002 .czexp: ZEN carrier "Insert - 3x Slide - Chamber
# Slide 8x - 432332-9110-000". The file records only the slide, not the insert.
INSERT_XML = """<HardwareExperiment>
  <SampleHolder>
    <Template Name="Chamber Slide 8x - 76 x 26 mm - Vertical">
      <AnchorPoint>0,0</AnchorPoint>
      <BoundsSize>26000,76000</BoundsSize>
      <Category>Multichamber</Category>
      <ShapeType>Rectangle</ShapeType>
      <ShapeColumns>2</ShapeColumns>
      <ShapeRows>4</ShapeRows>
      <ShapeWidth>9500</ShapeWidth>
      <ShapeHeight>10600</ShapeHeight>
      <ShapeDistanceX>10900</ShapeDistanceX>
      <ShapeDistanceY>12100</ShapeDistanceY>
      <BottomMaterial>Glass</BottomMaterial>
      <BottomThickness>186</BottomThickness>
      <BottomOffsetMeasured>NaN</BottomOffsetMeasured>
      <InsertType>PartOfInsert</InsertType>
      <PartOfInsertName></PartOfInsertName>
    </Template>
  </SampleHolder>
</HardwareExperiment>"""


def _write(tmp_path, xml, name="synthetic.czexp"):
    f = tmp_path / name
    f.write_text(xml, encoding="utf-8")
    return f


def test_sample_carrier_insert_records_only_the_slide(tmp_path):
    c = helper.get_sample_carrier_from_czexp(_write(tmp_path, INSERT_XML))
    assert c["name"] == "Chamber Slide 8x - 76 x 26 mm - Vertical"
    assert (c["rows"], c["columns"]) == (4, 2)
    assert c["well_width_m"] == pytest.approx(9.5e-3)
    assert c["bounds_size_m"] == pytest.approx((0.026, 0.076))
    assert c["bottom_thickness_m"] == pytest.approx(186e-6)
    assert c["insert_type"] == "PartOfInsert"
    assert c["is_part_of_insert"] is True
    assert c["part_of_insert_name"] is None


def test_sample_carrier_falls_back_to_czsht_copy(tmp_path):
    xml = """<X><AdditionalSettings><ScanAreaSampleHolderInfo>
      <Template Name="Multiwell 96.czsht"><ShapeRows>8</ShapeRows></Template>
    </ScanAreaSampleHolderInfo></AdditionalSettings></X>"""
    c = helper.get_sample_carrier_from_czexp(_write(tmp_path, xml))
    assert c["name"] == "Multiwell 96"
    assert c["rows"] == 8
    assert c["pitch_x_m"] is None           # missing tag → None


def test_sample_carrier_conflicting_names_warn(tmp_path, caplog):
    xml = """<X>
      <SampleHolder><Template Name="Multiwell 96" /></SampleHolder>
      <ScanAreaSampleHolderInfo><Template Name="Multichamber 384.czsht" /></ScanAreaSampleHolderInfo>
    </X>"""
    with caplog.at_level(logging.WARNING, logger="smartmic"):
        c = helper.get_sample_carrier_from_czexp(_write(tmp_path, xml))
    assert c["name"] == "Multiwell 96"      # the experiment's own SampleHolder wins
    assert "more than one sample carrier" in caplog.text


def test_sample_carrier_none_without_template(tmp_path):
    xml = "<X><SampleHolder><Overlap>0.1</Overlap></SampleHolder></X>"
    assert helper.get_sample_carrier_from_czexp(_write(tmp_path, xml)) is None


def test_sample_carrier_missing_files_return_none(tmp_path):
    assert helper.get_sample_carrier_from_czexp(tmp_path / "nope.czexp") is None
    assert helper.get_sample_carrier_from_czi(tmp_path / "nope.czi") is None


def test_check_sample_carrier_plate():
    plate = {"name": "Marc_96W_U-Bottom", "is_part_of_insert": False}
    assert helper.check_sample_carrier(plate, "Marc_96W_U-Bottom")[0] is True
    assert helper.check_sample_carrier(plate, "Multiwell 96")[0] is False
    assert helper.check_sample_carrier(plate)[0] is None
    # expected_slide is irrelevant for a plate
    assert helper.check_sample_carrier(plate, "Marc_96W_U-Bottom", "x")[0] is True


def test_check_sample_carrier_insert():
    slide = {"name": "Chamber Slide 8x - 76 x 26 mm - Vertical", "is_part_of_insert": True}
    insert = "Insert - 3x Slide - Chamber Slide 8x - 432332-9110-000"
    # The insert name is never in the file: can't check, never a false mismatch.
    ok, msg = helper.check_sample_carrier(slide, insert)
    assert ok is None and "can't be checked" in msg
    assert helper.check_sample_carrier(
        slide, insert, "Chamber Slide 8x - 76 x 26 mm - Vertical")[0] is True
    assert helper.check_sample_carrier(
        slide, insert, "Slide 76 x 26 mm - Vertical")[0] is False


def test_check_sample_carrier_no_carrier():
    assert helper.check_sample_carrier(None, "Multiwell 96")[0] is None
