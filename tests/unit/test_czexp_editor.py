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


def _zstack_value(root, tag):
    zs = cz.find_zstack_setup(root)
    return float(zs.find(tag).find("Distance").find("Value").text)


def test_set_zstack_range_expect_mode_match(czexp_dir):
    _, root = cz.load_czexp(czexp_dir / "positions_384.czexp")     # FixedZ
    cz.set_zstack_range(root, 2856e-6, 3256e-6, 5e-6, expect_mode="FixedZ")
    assert _zstack_value(root, "First") == pytest.approx(2856e-6)
    assert _zstack_value(root, "Last") == pytest.approx(3256e-6)


def test_set_zstack_range_expect_mode_mismatch_writes_nothing(czexp_dir):
    # The Marc_SM_003 incident: a relative ±200 µm range into a FixedZ file
    # would run at absolute Z −200 … +200 µm.  Must be refused, file untouched.
    _, root = cz.load_czexp(czexp_dir / "positions_384.czexp")     # FixedZ
    before = (_zstack_value(root, "First"), _zstack_value(root, "Last"),
              _zstack_value(root, "Interval"))
    with pytest.raises(ValueError, match="FocusZStackMode is 'FixedZ'"):
        cz.set_zstack_range(root, -200e-6, 200e-6, 5e-6, expect_mode="CurrentZ")
    after = (_zstack_value(root, "First"), _zstack_value(root, "Last"),
             _zstack_value(root, "Interval"))
    assert after == before


def test_set_zstack_range_expect_mode_missing_field_refused(synth_root):
    # No FocusZStackMode → can't tell relative from absolute → refuse.
    with pytest.raises(ValueError, match="None"):
        cz.set_zstack_range(synth_root, -5e-6, 5e-6, expect_mode="CurrentZ")


def test_set_zstack_range_expect_mode_bad_value(lsm_root):
    with pytest.raises(ValueError, match="expected must be one of"):
        cz.set_zstack_range(lsm_root, -5e-6, 5e-6, expect_mode="relative")


# --------------------------------------------------------------------------
# FocusZStackMode: decides relative (CurrentZ) vs absolute (FixedZ) First/Last
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name,expected", [
    ("zstack_LSM.czexp", "CurrentZ"),        # CENTER mode, relative
    ("positions_384.czexp", "FixedZ"),
    ("swaf_fine_20x.czexp", "CurrentZ"),     # has an embedded SWAF experiment too
])
def test_get_focus_z_stack_mode_real(czexp_dir, name, expected):
    _, root = cz.load_czexp(czexp_dir / name)
    assert cz.get_focus_z_stack_mode(root) == expected


def test_get_focus_z_stack_mode_missing(synth_root):
    assert cz.get_focus_z_stack_mode(synth_root) is None


def _mode_xml(*blocks, embedded=None):
    """Acquisition blocks with the given modes; optionally an embedded
    (software-autofocus) experiment with its own mode."""
    def fs(mode, inner=""):
        return (f"<FocusSetup><FocusStrategy><FocusZStackMode>{mode}</FocusZStackMode>"
                f"</FocusStrategy>{inner}</FocusSetup>")
    inner = ""
    if embedded is not None:
        inner = (f"<SoftwareAutofocus><Extensions><Extension><Experiment><ExperimentBlocks>"
                 f"<AcquisitionBlock><HelperSetups>{fs(embedded)}</HelperSetups>"
                 f"<SubDimensionSetups><ZStackSetup>"
                 f"<First><Distance><Value>-9</Value></Distance></First>"
                 f"<Last><Distance><Value>9</Value></Distance></Last>"
                 f"</ZStackSetup></SubDimensionSetups>"
                 f"</AcquisitionBlock></ExperimentBlocks></Experiment></Extension>"
                 f"</Extensions></SoftwareAutofocus>")
    body = "".join(f"<AcquisitionBlock><HelperSetups>{fs(m, inner)}</HelperSetups>"
                   f"</AcquisitionBlock>" for m in blocks)
    return ET.fromstring(f"<HardwareExperiment><ExperimentBlocks>{body}</ExperimentBlocks>"
                         f"</HardwareExperiment>")


def test_get_focus_z_stack_mode_ignores_embedded_autofocus_experiment():
    root = _mode_xml("FixedZ", embedded="CurrentZ")
    assert cz.get_focus_z_stack_mode(root) == "FixedZ"


def test_get_focus_z_stack_mode_conflicting_blocks_raise():
    with pytest.raises(ValueError, match="conflicting"):
        cz.get_focus_z_stack_mode(_mode_xml("FixedZ", "CurrentZ"))


def test_find_zstack_setup_skips_embedded_autofocus_stack():
    # Only the SWAF's own z-stack has First/Last → it must NOT be picked up
    # (set_zstack_range would otherwise edit the autofocus range).
    with pytest.raises(ValueError, match="not found"):
        cz.find_zstack_setup(_mode_xml("CurrentZ", embedded="CurrentZ"))


def test_require_focus_z_stack_mode(lsm_root):
    assert cz.require_focus_z_stack_mode(lsm_root, "CurrentZ") == "CurrentZ"
    with pytest.raises(ValueError, match=r"x\.czexp: FocusZStackMode is 'CurrentZ'"):
        cz.require_focus_z_stack_mode(lsm_root, "FixedZ", path="x.czexp")


def test_is_zstack_center_mode(lsm_root, czexp_dir):
    assert cz.is_zstack_center_mode(lsm_root) is True
    _, root = cz.load_czexp(czexp_dir / "positions_384.czexp")
    assert cz.is_zstack_center_mode(root) is False


def test_summarize_reports_z_modes(lsm_root):
    s = cz.summarize(lsm_root)
    assert s["focus_z_stack_mode"] == "CurrentZ"
    assert s["zstack_center_mode"] is True


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


# --------------------------------------------------------------------------
# Focus strategy detection (ZEN can silently re-add one on re-open/save)
# --------------------------------------------------------------------------
def _focus_xml(method="None", search="None", surface="None",
               setup_act="true", strategy_act="true"):
    return (f'<HardwareExperiment><FocusSetup IsActivated="{setup_act}">'
            f'<FocusStrategy IsActivated="{strategy_act}">'
            f'<Method>{method}</Method><SearchAction>{search}</SearchAction>'
            f'<SurfaceMode>{surface}</SurfaceMode>'
            f'</FocusStrategy></FocusSetup></HardwareExperiment>')


def test_has_focus_strategy_active_method():
    # find_tissue / find_thickness shape: a real SearchAction is configured.
    root = ET.fromstring(_focus_xml(method="FollowAction",
                                    search="DefiniteFocusFindSurface"))
    assert cz.has_focus_strategy(root) is True
    assert cz.get_focus_strategy(root)["search_action"] == "DefiniteFocusFindSurface"


def test_has_focus_strategy_all_none_is_off():
    # find_nuclei / image_nuclei shape: Method/SearchAction/SurfaceMode all None.
    root = ET.fromstring(_focus_xml())
    assert cz.has_focus_strategy(root) is False
    assert cz.get_focus_strategy(root) is None


def test_has_focus_strategy_missing_setup():
    assert cz.has_focus_strategy(ET.fromstring("<HardwareExperiment/>")) is False


def test_has_focus_strategy_deactivated_setup_or_strategy():
    # An otherwise-real strategy that is deactivated does not count.
    off_setup = ET.fromstring(_focus_xml(method="FollowAction", setup_act="false"))
    off_strat = ET.fromstring(_focus_xml(method="FollowAction", strategy_act="false"))
    assert cz.has_focus_strategy(off_setup) is False
    assert cz.has_focus_strategy(off_strat) is False


# --------------------------------------------------------------------------
# Run-safety checks: require_no_focus_search, require_runnable_tiles
# --------------------------------------------------------------------------
def test_require_no_focus_search_allows_follow_surface_without_search():
    # Marc_SM_003's OV bases: FollowSurface, no search → uses the region Z.
    root = ET.fromstring(_focus_xml(method="FollowSurface", surface="Local"))
    assert cz.require_no_focus_search(root)["method"] == "FollowSurface"
    assert cz.require_no_focus_search(ET.fromstring(_focus_xml())) is None


@pytest.mark.parametrize("search", ["DefiniteFocusFindSurface", "SoftwareAutofocus"])
def test_require_no_focus_search_refuses_a_search(search):
    root = ET.fromstring(_focus_xml(method="FollowAction", search=search))
    with pytest.raises(ValueError, match=rf"x\.czexp: .*focus search.*{search}.*\(U-bottom\)"):
        cz.require_no_focus_search(root, "x.czexp", reason="U-bottom")


@pytest.mark.parametrize("name,refused", [
    ("zstack_LSM.czexp", False), ("positions_384.czexp", False),
    ("swaf_fine_20x.czexp", True), ("stitch_region.czexp", True)])   # SoftwareAutofocus search
def test_require_no_focus_search_real(czexp_dir, name, refused):
    _, root = cz.load_czexp(czexp_dir / name)
    if refused:
        with pytest.raises(ValueError, match="SoftwareAutofocus"):
            cz.require_no_focus_search(root)
    else:
        cz.require_no_focus_search(root)


def _tiles_xml(activated, regions="", embedded=None):
    """Own TilesSetup (+ regions); optionally an embedded autofocus experiment
    whose TilesSetup is (activated, regions)."""
    inner = ""
    if embedded is not None:
        act, reg = embedded
        inner = (f'<SoftwareAutofocus><Experiment><TilesSetup IsActivated="{act}">{reg}'
                 f'</TilesSetup></Experiment></SoftwareAutofocus>')
    return ET.fromstring(f'<HardwareExperiment><TilesSetup IsActivated="{activated}">{regions}'
                         f'</TilesSetup>{inner}</HardwareExperiment>')


TILE = '<TileRegions><TileRegion Name="B2" /></TileRegions>'
SINGLE = '<SingleTileRegions><SingleTileRegion Name="P1" /></SingleTileRegions>'


def test_require_runnable_tiles_refuses_tiles_on_without_region():
    root = _tiles_xml("true", "<TileRegions /><SingleTileRegions />")
    assert cz.tiles_activated(root) is True
    with pytest.raises(ValueError, match=r"final\.czexp: Tiles is switched on but there is no tile region"):
        cz.require_runnable_tiles(root, "final.czexp")


@pytest.mark.parametrize("root", [
    _tiles_xml("false"),                       # Tiles off → acquires at the stage position
    _tiles_xml("true", TILE),
    _tiles_xml("true", SINGLE),
    ET.fromstring("<HardwareExperiment />"),   # no TilesSetup at all
])
def test_require_runnable_tiles_passes(root):
    cz.require_runnable_tiles(root)


def test_require_runnable_tiles_ignores_embedded_autofocus_experiment():
    # The autofocus sub-experiment's region does not make the outer run startable …
    with pytest.raises(ValueError, match="no tile region"):
        cz.require_runnable_tiles(_tiles_xml("true", embedded=("true", TILE)))
    # … and its Tiles-on-without-region does not block an outer run with Tiles off.
    root = _tiles_xml("false", embedded=("true", ""))
    assert cz.tiles_activated(root) is False
    cz.require_runnable_tiles(root)


@pytest.mark.parametrize("name", ["zstack_LSM.czexp", "positions_384.czexp", "stitch_region.czexp",
                                  "swaf_fine_20x.czexp", "zstack_small.czexp", "snap_single.czexp"])
def test_require_runnable_tiles_real_fixtures(czexp_dir, name):
    _, root = cz.load_czexp(czexp_dir / name)
    cz.require_runnable_tiles(root)


# --------------------------------------------------------------------------
# get_tile_region_centers
# --------------------------------------------------------------------------
def test_get_tile_region_centers_real(stitch_root):
    # stitch_region.czexp: one region "I13" at CenterPosition 56250,42250 µm.
    assert cz.get_tile_region_centers(stitch_root) == {
        "I13": (pytest.approx(0.05625), pytest.approx(0.04225))}


def _regions_xml(*regions):
    body = "".join(
        f'<TileRegion{f" Name={name!r}" if name else ""}>'
        + (f"<CenterPosition>{cp}</CenterPosition>" if cp else "")
        + (f"<IsUsedForAcquisition>{used}</IsUsedForAcquisition>" if used else "")
        + "</TileRegion>"
        for name, cp, used in regions)
    return ET.fromstring(f"<HardwareExperiment><TileRegions>{body}</TileRegions>"
                         f"<SingleTileRegions><SingleTileRegion Name='P1'><X>1</X><Y>2</Y>"
                         f"</SingleTileRegion></SingleTileRegions></HardwareExperiment>")


def test_get_tile_region_centers_skips_unused_keeps_order():
    root = _regions_xml(("B1", "7300,31200", None),        # no flag → used
                        ("A2", "18200,19100", "false"),    # skipped
                        ("A1", "7300,19100", "True"))
    centers = cz.get_tile_region_centers(root)
    assert list(centers) == ["B1", "A1"]                    # file order; no SingleTileRegion
    assert centers["A1"] == (pytest.approx(7300e-6), pytest.approx(19100e-6))


def test_get_tile_region_centers_duplicate_name_raises():
    root = _regions_xml(("A1", "1,2", None), ("A1", "3,4", None))
    with pytest.raises(ValueError, match="A1"):
        cz.get_tile_region_centers(root)


def test_get_tile_region_centers_duplicate_unused_is_fine():
    root = _regions_xml(("A1", "1,2", None), ("A1", "3,4", "false"))
    assert list(cz.get_tile_region_centers(root)) == ["A1"]


@pytest.mark.parametrize("region", [(None, "1,2", None), ("A1", None, None)])
def test_get_tile_region_centers_incomplete_region_raises(region):
    with pytest.raises(ValueError):
        cz.get_tile_region_centers(_regions_xml(region))


# --------------------------------------------------------------------------
# get_tile_region_shape_ids / retarget_tile_region (re-aim a region at a well)
# --------------------------------------------------------------------------
def test_get_tile_region_shape_ids_real(stitch_root):
    assert cz.get_tile_region_shape_ids(stitch_root) == {"I13": "9-13"}


def test_get_tile_region_shape_ids_selection_and_missing_id():
    root = ET.fromstring(
        "<HardwareExperiment><TileRegions>"
        "<TileRegion Name='AA1'><TemplateShapeId>27-1</TemplateShapeId></TileRegion>"
        "<TileRegion Name='B2'><TemplateShapeId>2-2</TemplateShapeId>"
        "<IsUsedForAcquisition>false</IsUsedForAcquisition></TileRegion>"
        "<TileRegion Name='X'></TileRegion>"
        "</TileRegions></HardwareExperiment>")
    # The name is never parsed: a 1536-style row name works like any other.
    assert cz.get_tile_region_shape_ids(root) == {"AA1": "27-1", "X": None}
    with pytest.raises(ValueError, match="A1"):
        cz.get_tile_region_shape_ids(_regions_xml(("A1", "1,2", None), ("A1", "3,4", None)))


def test_retarget_tile_region_rewrites_all_three_links(stitch_root):
    tiling = {t: stitch_root.find(".//TileRegion").findtext(t) for t in ("Columns", "Rows", "ContourSize")}
    old = cz.retarget_tile_region(stitch_root, "AF48", 0.1, 0.02, z_m=3000e-6, shape_id="32-48")
    assert old == {"authored_name": "I13", "authored_shape_id": "9-13"}
    reg = stitch_root.find(".//TileRegion")
    assert reg.get("Name") == "AF48" and reg.findtext("TemplateShapeId") == "32-48"
    assert cz.get_tile_region_centers(stitch_root) == {"AF48": (pytest.approx(0.1), pytest.approx(0.02))}
    assert float(reg.findtext("Z")) == pytest.approx(3000.0)        # was 2985.683
    assert {t: reg.findtext(t) for t in tiling} == tiling          # tiling untouched


def test_retarget_tile_region_refuses_a_stale_container_link(stitch_root):
    with pytest.raises(ValueError, match="9-13"):
        cz.retarget_tile_region(stitch_root, "B2", 0.01, 0.02)
    assert stitch_root.find(".//TileRegion").get("Name") == "I13"   # nothing changed


@pytest.mark.parametrize("bad", ["B2", "2_2", "", "2-"])
def test_retarget_tile_region_refuses_a_malformed_shape_id(stitch_root, bad):
    with pytest.raises(ValueError, match="row-column"):
        cz.retarget_tile_region(stitch_root, "B2", 0.01, 0.02, shape_id=bad)


def test_retarget_tile_region_without_container_link():
    root = _regions_xml(("A1", "1,2", None))
    assert cz.retarget_tile_region(root, "B2", 0.01, 0.02) == {"authored_name": "A1",
                                                              "authored_shape_id": None}
    reg = root.find(".//TileRegion")
    assert reg.get("Name") == "B2" and reg.find("TemplateShapeId") is None   # none added
