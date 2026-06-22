# -*- coding: utf-8 -*-
"""Unit tests for MS_czexp_editor — pure-stdlib .czexp read/modify (no ZEN API).

A real confocal fixture (``zstack_LSM.czexp``) exercises the LSM / z-stack /
region functions against an actual ZEN file; synthetic XML covers the
exact-value logic (crop math, run-mode stripping, stitching detection)
deterministically.
"""
import xml.etree.ElementTree as ET

import pytest

import MS_czexp_editor as cz


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def lsm_path(czexp_dir):
    return czexp_dir / "zstack_LSM.czexp"


@pytest.fixture
def lsm_root(lsm_path):
    _, root = cz.load_czexp(lsm_path)
    return root


@pytest.fixture
def stitch_root(czexp_dir):
    _, root = cz.load_czexp(czexp_dir / "stitch_region.czexp")
    return root


# A self-contained synthetic LSM experiment for deterministic crop tests.
# base pixel = 414.72 / 1024 = 0.405 µm.
SYNTH_LSM = """<HardwareExperiment>
  <RunMode>A,OptimizeBeforePerformEnabled,ValidateAndAdaptBeforePerformEnabled,B</RunMode>
  <ZStackSetup>
    <First><Distance><Value>-1E-05</Value></Distance></First>
    <Last><Distance><Value>1E-05</Value></Distance></Last>
    <Interval><Distance><Value>2E-06</Value></Distance></Interval>
  </ZStackSetup>
  <SingleTileRegions>
    <SingleTileRegion Name="P1" Id="1">
      <X>1000.0</X><Y>2000.0</Y><Z>3000.0</Z>
    </SingleTileRegion>
  </SingleTileRegions>
  <Detectors>
    <Detector Id="MTBLSMImagingDevice">
      <FrameSize>1024,1024</FrameSize>
      <ScaledImageRectangleSize>414.72,414.72</ScaledImageRectangleSize>
      <ScaledImageRectangle>0,0,414.72,414.72</ScaledImageRectangle>
      <ScaledMaxImageSize>414.72,414.72</ScaledMaxImageSize>
      <Zoom>1.0,1.0</Zoom>
      <Sampling>1.0</Sampling>
      <SamplingMode>SR</SamplingMode>
      <IsMaxScanSpeedSet>false</IsMaxScanSpeedSet>
      <Frame>0,0,1024,1024</Frame>
      <ImageFrame>0,0,1024,1024</ImageFrame>
    </Detector>
  </Detectors>
</HardwareExperiment>"""


@pytest.fixture
def synth_root():
    return ET.fromstring(SYNTH_LSM)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def test_fmt_float_roundtrip():
    assert cz._fmt_float(1) == "1.0"
    assert float(cz._fmt_float(0.00111297)) == 0.00111297
    assert float(cz._fmt_float(-5e-6)) == -5e-6


def test_parse_pair():
    assert cz._parse_pair("1859,1859") == (1859.0, 1859.0)
    assert cz._parse_pair("1.5,2.5,extra") == (1.5, 2.5)


def test_require_returns_child_and_raises():
    el = ET.fromstring("<R><A>x</A></R>")
    assert cz._require(el, "A").text == "x"
    with pytest.raises(ValueError):
        cz._require(el, "B")


# --------------------------------------------------------------------------
# Load / save
# --------------------------------------------------------------------------
def test_load_czexp_root_tag(lsm_root):
    assert lsm_root.tag == "HardwareExperiment"


def test_save_czexp_roundtrip(lsm_path, tmp_path):
    tree, root = cz.load_czexp(lsm_path)
    n_before = len(cz.find_tile_regions(root))
    out = tmp_path / "out.czexp"
    cz.save_czexp(tree, out)
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")        # UTF-8 BOM (ZEN style)
    assert b"<?xml" in raw[:200]                  # XML declaration
    _, root2 = cz.load_czexp(out)
    assert len(cz.find_tile_regions(root2)) == n_before


# --------------------------------------------------------------------------
# Locators
# --------------------------------------------------------------------------
def test_find_lsm_detector_real(lsm_root):
    assert cz.find_lsm_detector(lsm_root).get("Id") == cz.LSM_DETECTOR_ID


def test_find_lsm_detector_missing_raises():
    with pytest.raises(ValueError):
        cz.find_lsm_detector(ET.fromstring("<HardwareExperiment/>"))


def test_find_tile_regions_real(lsm_root):
    assert len(cz.find_tile_regions(lsm_root)) == 1


def test_find_stitch_regions(stitch_root):
    assert len(cz.find_stitch_regions(stitch_root)) >= 1


def test_find_zstack_setup_real(lsm_root):
    zs = cz.find_zstack_setup(lsm_root)
    assert zs.find("First") is not None and zs.find("Last") is not None


def test_find_processing_steps_returns_list(lsm_root):
    assert isinstance(cz.find_processing_steps(lsm_root), list)


@pytest.mark.parametrize("xml,expected", [
    ("<HardwareExperiment/>", True),
    ('<HardwareExperiment><ExperimentRemoteProcessingSetup IsActivated="false">'
     '<ProcessingStep Id="NULL"/></ExperimentRemoteProcessingSetup></HardwareExperiment>', True),
    ('<HardwareExperiment><ExperimentRemoteProcessingSetup IsActivated="true">'
     '<ProcessingStep Id="NULL"/></ExperimentRemoteProcessingSetup></HardwareExperiment>', False),
    ('<HardwareExperiment><ExperimentRemoteProcessingSetup IsActivated="true">'
     '<ProcessingStep Id="real"/></ExperimentRemoteProcessingSetup></HardwareExperiment>', True),
    ('<HardwareExperiment><ExperimentRemoteProcessingSetup IsActivated="true">'
     '<ProcessingStep Id="NULL"><Algorithm/></ProcessingStep>'
     '</ExperimentRemoteProcessingSetup></HardwareExperiment>', True),
])
def test_is_stitching_configured(xml, expected):
    assert cz.is_stitching_configured(ET.fromstring(xml)) is expected


# --------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------
def test_get_lsm_pixel_size_um_real(lsm_root):
    assert cz.get_lsm_pixel_size_um(lsm_root) > 0


def test_get_lsm_pixel_size_um_synth(synth_root):
    assert cz.get_lsm_pixel_size_um(synth_root) == pytest.approx(414.72 / 1024)


def test_get_zstack_interval_m_real(lsm_root):
    assert cz.get_zstack_interval_m(lsm_root) > 0


def test_summarize_keys(lsm_root):
    s = cz.summarize(lsm_root)
    for k in ("regions", "zstack_first_m", "zstack_last_m", "zstack_interval_m",
              "zoom", "frame_size", "sampling", "pixel_um"):
        assert k in s
    assert len(s["regions"]) == len(cz.find_tile_regions(lsm_root))
    assert s["pixel_um"] == pytest.approx(cz.get_lsm_pixel_size_um(lsm_root))


# --------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------
def test_set_position_writes_um(lsm_root):
    cz.set_position(lsm_root, 0.05, 0.06, 0.07, region_index=0)
    r = cz.find_tile_regions(lsm_root)[0]
    assert r.find("X").text == cz._fmt_float(50000.0)
    assert r.find("Y").text == cz._fmt_float(60000.0)
    assert r.find("Z").text == cz._fmt_float(70000.0)


def test_set_position_no_region_raises():
    with pytest.raises(ValueError):
        cz.set_position(ET.fromstring("<HardwareExperiment/>"), 0.1, 0.1)


def test_add_single_tile_region_updates_existing(lsm_root):
    n = len(cz.find_tile_regions(lsm_root))
    cz.add_single_tile_region(lsm_root, 0.01, 0.02, 0.03)
    assert len(cz.find_tile_regions(lsm_root)) == n  # updated in place, not added


def test_add_single_tile_region_creates_when_empty():
    root = ET.fromstring("<HardwareExperiment><SingleTileRegions/></HardwareExperiment>")
    assert cz.find_tile_regions(root) == []
    cz.add_single_tile_region(root, 0.01, 0.02, 0.03, name="P9")
    regions = cz.find_tile_regions(root)
    assert len(regions) == 1
    assert regions[0].get("Name") == "P9"
    assert regions[0].find("X").text == cz._fmt_float(10000.0)
    assert regions[0].find("IsUsedForAcquisition").text == "true"


def test_set_tile_region_center(stitch_root):
    cz.set_tile_region_center(stitch_root, 0.04, 0.05)
    cp = cz.find_stitch_regions(stitch_root)[0].find("CenterPosition").text
    assert cp == f"{cz._fmt_float(40000.0)},{cz._fmt_float(50000.0)}"


def test_clear_single_tile_regions(lsm_root):
    removed = cz.clear_single_tile_regions(lsm_root)
    assert removed > 0
    assert cz.find_tile_regions(lsm_root) == []


def test_set_zstack_range(synth_root):
    cz.set_zstack_range(synth_root, -5e-6, 5e-6, 1e-6)
    assert cz.get_zstack_interval_m(synth_root) == pytest.approx(1e-6)
    zs = cz.find_zstack_setup(synth_root)
    first = float(zs.find("First").find("Distance").find("Value").text)
    last = float(zs.find("Last").find("Distance").find("Value").text)
    assert first == pytest.approx(-5e-6)
    assert last == pytest.approx(5e-6)


def test_fit_lsm_crop_constant_pixel(synth_root):
    base = cz.get_lsm_pixel_size_um(synth_root)
    applied = cz.fit_lsm_crop(synth_root, 207.36)
    assert cz.FRAME_SIZE_MIN <= applied["frame_size"] <= cz.FRAME_SIZE_MAX
    assert applied["frame_size"] == 512                     # 207.36 / 0.405
    assert applied["pixel_um"] == pytest.approx(base, rel=1e-3)
    assert applied["sampling_mode"] is None                 # SamplingMode untouched


def test_fit_lsm_crop_fixed_frame(synth_root):
    applied = cz.fit_lsm_crop(synth_root, 207.36, frame_size_override=256)
    assert applied["frame_size"] == 256
    assert applied["pixel_um"] == pytest.approx(207.36 / 256)
    assert applied["sampling_mode"] == "User"
    assert cz.find_lsm_detector(synth_root).find("SamplingMode").text == "User"


def test_fit_lsm_crop_real_fixed_frame(lsm_root):
    applied = cz.fit_lsm_crop(lsm_root, 200.0, frame_size_override=512)
    assert applied["frame_size"] == 512
    assert applied["pixel_um"] == pytest.approx(200.0 / 512)


def test_set_run_mode_lock(synth_root):
    assert cz.set_run_mode_lock(synth_root) == "A,B"


def test_set_run_mode_lock_absent_returns_none():
    assert cz.set_run_mode_lock(ET.fromstring("<HardwareExperiment/>")) is None


def test_set_lsm_scan_speed_max(synth_root):
    assert cz.set_lsm_scan_speed_max(synth_root) is True
    assert cz.find_lsm_detector(synth_root).find("IsMaxScanSpeedSet").text == "true"


def test_set_lsm_scan_speed_max_no_field():
    root = ET.fromstring('<HardwareExperiment><Detector Id="MTBLSMImagingDevice"/></HardwareExperiment>')
    assert cz.set_lsm_scan_speed_max(root) is False


def test_set_lsm_sampling_mode_user(synth_root):
    assert cz.set_lsm_sampling_mode_user(synth_root) == "1024,1024"
    assert cz.find_lsm_detector(synth_root).find("SamplingMode").text == "User"


def test_set_lsm_sampling_mode_user_no_field():
    root = ET.fromstring('<HardwareExperiment><Detector Id="MTBLSMImagingDevice"/></HardwareExperiment>')
    assert cz.set_lsm_sampling_mode_user(root) is None
