# -*- coding: utf-8 -*-

#################################################################
# File        : MS_czexp_editor.py
# Author      : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#
# Copyright(c) 2026 ETH Zurich (ScopeM). All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

"""
MS_czexp_editor.py  —  read and modify ZEN .czexp experiment files.

Pure stdlib (xml.etree) — no ZEN API, no extra dependency.  Generic helpers for
any project that needs to customise an experiment per target (position, z-stack,
scan crop) and then run the result via the LoA's run_experiment_from_path.

A .czexp is a ZEN HardwareExperiment XML.  The fields this module edits:

  POSITION   SingleTileRegion "P1"  → <X> <Y> <Z>          (stage coords in µm)
  Z-STACK    ZStackSetup            → <First>/<Last>/<Interval> <Value>
                                       (values are in METRES despite the "um" label)
  CROP       Detector "MTBLSMImagingDevice"
                                     → <Zoom> <FrameSize> <Frame> <ImageFrame>
                                       <Sampling> <SamplingMode>
                                       <ScaledImageRectangle(Size)> <ScaledMaxImageSize>
  SCAN SPEED Detector "MTBLSMImagingDevice" → <IsMaxScanSpeedSet>
  RUN MODE   HardwareExperiment     → <RunMode>

Crop: Zoom sets the field of view (FOV); FrameSize sets the sampling within it.
Two modes (see fit_lsm_crop):
  * constant pixel size  → FrameSize = round(FOV / base_pixel), SamplingMode left
                           as-is (Nyquist as in the base).
  * fixed frame size     → FrameSize forced (e.g. 512² fast preview); Sampling is
                           set to match AND SamplingMode is switched to "User".

ZEN gotchas these helpers handle (all learned against the live system):
  * The CENTER-mode z-stack is built around the tile-region <Z>, not the
    First/Last midpoint — so set_position writes <Z> = target centre Z.
  * Frame size is DERIVED from FOV × Sampling and is recomputed on load under
    SamplingMode=SR; forcing a manual frame requires SamplingMode=User + a
    matching Sampling value (this is how the FAZ experiment keeps its 512²).
  * Generated files are run via ExperimentService.Load BY PATH (see the LoA's
    run_experiment_from_path) — load, do NOT import-then-run.

Always give a generated file a unique name (ZEN caches by name on re-import) and
verify it once on the scope before trusting a batch run.

CLI:
    python MS_czexp_editor.py <file.czexp>              # summarize
    python MS_czexp_editor.py <file.czexp> fit <fov_um> # dry-run crop fit
"""

import codecs
import xml.etree.ElementTree as ET
from pathlib import Path

LSM_DETECTOR_ID = "MTBLSMImagingDevice"

# LSM frame-size hardware limits (from FrameSizeMin/Max in the base czexp).
FRAME_SIZE_MIN = 32
FRAME_SIZE_MAX = 5120

# FocusStrategy field values that mean "no strategy is doing anything".
FOCUS_OFF_VALUES = {"", "None", "NONE", "none"}


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_czexp(path):
    """Parse a .czexp file and return (tree, root)."""
    path = Path(path)
    tree = ET.parse(path)
    return tree, tree.getroot()


def save_czexp(tree, path):
    """Write a .czexp file as UTF-8 with BOM + XML declaration (ZEN style)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    xml_bytes = ET.tostring(tree.getroot(), encoding="utf-8", xml_declaration=True)
    with open(path, "wb") as f:
        f.write(codecs.BOM_UTF8)
        f.write(xml_bytes)
    return path


# ---------------------------------------------------------------------------
# Small parsing/formatting helpers
# ---------------------------------------------------------------------------

def _fmt_float(v):
    """Shortest round-trippable float string (e.g. 0.00111297)."""
    return repr(float(v))


def _parse_pair(text):
    """'1859,1859' -> (1859.0, 1859.0)."""
    a, b = text.split(",")[:2]
    return float(a), float(b)


def _require(elem, tag):
    child = elem.find(tag)
    if child is None:
        raise ValueError(f"<{tag}> not found under <{elem.tag}>")
    return child


# ---------------------------------------------------------------------------
# Element locators
# ---------------------------------------------------------------------------

def find_lsm_detector(root):
    """Return the <Detector Id='MTBLSMImagingDevice'> element."""
    for det in root.iter("Detector"):
        if det.get("Id") == LSM_DETECTOR_ID:
            return det
    raise ValueError(f"LSM detector '{LSM_DETECTOR_ID}' not found")


def find_tile_regions(root):
    """Return all <SingleTileRegion> elements (single-position acquisitions)."""
    return list(root.iter("SingleTileRegion"))


def find_stitch_regions(root):
    """Return all <TileRegion> elements (multi-tile / stitch regions).

    A stitch region is positioned by its <CenterPosition> (µm) and sized by
    <ContourSize> / <Columns> / <Rows>, unlike a SingleTileRegion (<X>/<Y>).
    """
    return list(root.iter("TileRegion"))


def find_zstack_setup(root):
    """Return the <ZStackSetup> element that defines First/Last/Interval."""
    for zs in root.iter("ZStackSetup"):
        if zs.find("First") is not None and zs.find("Last") is not None:
            return zs
    raise ValueError("ZStackSetup with First/Last not found")


def find_processing_steps(root):
    """Return all <ProcessingStep> elements (remote-processing / stitching steps)."""
    return list(root.iter("ProcessingStep"))


def is_stitching_configured(root):
    """False only if an ACTIVE remote-processing step is the empty placeholder.

    A stitch experiment saved with an active but unconfigured step
    (``<ProcessingStep Id="NULL"/>`` and no children) runs in the ZEN UI — ZEN
    fills the defaults on open — but the API run aborts ("experiment not valid /
    failed to start").  Returns True when:
      * there is no <ExperimentRemoteProcessingSetup>, or
      * it is IsActivated="false" (deactivated → nothing runs/validates), or
      * a real step (Id != "NULL", or with an Algorithm/FunctionParameters
        child) is present.
    """
    setup = next(iter(root.iter("ExperimentRemoteProcessingSetup")), None)
    if setup is None:
        return True
    if (setup.get("IsActivated") or "true").lower() == "false":
        return True
    steps = list(setup.iter("ProcessingStep"))
    if not steps:
        return True
    for s in steps:
        if (s.get("Id") or "NULL") != "NULL":
            return True
        if s.find("Algorithm") is not None or s.find("FunctionParameters") is not None:
            return True
    return False


def get_focus_strategy(root):
    """Return the active focus strategy as a dict, or None if there is none.

    The strategy lives in <FocusSetup><FocusStrategy> as <Method>, <SearchAction>
    and <SurfaceMode> (e.g. Method=FollowAction, SearchAction=DefiniteFocusFindSurface).
    Returns {"method", "search_action", "surface_mode"} when a real strategy is
    configured, else None.  See has_focus_strategy for the "what counts as a
    strategy" rules.
    """
    setup = next(iter(root.iter("FocusSetup")), None)
    if setup is None or (setup.get("IsActivated") or "true").lower() == "false":
        return None
    fs = setup.find("FocusStrategy")
    if fs is None or (fs.get("IsActivated") or "true").lower() == "false":
        return None
    fields = {tag: (fs.findtext(tag) or "").strip()
              for tag in ("Method", "SearchAction", "SurfaceMode")}
    if all(v in FOCUS_OFF_VALUES for v in fields.values()):
        return None
    return {"method": fields["Method"],
            "search_action": fields["SearchAction"],
            "surface_mode": fields["SurfaceMode"]}


def has_focus_strategy(root):
    """True if an ACTIVE focus strategy is configured in the experiment.

    ZEN can silently (re)add a focus strategy when an experiment is re-opened or
    re-saved.  Some pipeline stages forbid this — e.g. HD_Nuclei_from_slide's
    find_nuclei / image_nuclei set Z explicitly and MUST run with no strategy.
    The strategy is effectively OFF when its <Method>, <SearchAction> and
    <SurfaceMode> are all "None" (the ZEN "no strategy" state), even though
    <FocusStrategy> keeps IsActivated="true".  Returns True when the setup is
    activated and any of those three names a real action.  Check-only: use it to
    assert/abort before running a generated .czexp.
    """
    return get_focus_strategy(root) is not None


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def get_lsm_pixel_size_um(root):
    """Current LSM pixel size in µm = ScaledImageRectangleSize / FrameSize."""
    det = find_lsm_detector(root)
    rect_x, _ = _parse_pair(_require(det, "ScaledImageRectangleSize").text)
    frame_x, _ = _parse_pair(_require(det, "FrameSize").text)
    return rect_x / frame_x


def get_zstack_interval_m(root):
    """Current z-stack interval (step) in metres."""
    zs = find_zstack_setup(root)
    return float(_require(_require(zs, "Interval"), "Distance").find("Value").text)


def summarize(root):
    """Return a dict of the current values this module can edit (for logging)."""
    det = find_lsm_detector(root)
    zs = find_zstack_setup(root)
    regions = find_tile_regions(root)

    def zval(tag):
        return float(_require(_require(zs, tag), "Distance").find("Value").text)

    out = {
        "regions": [
            {
                "name": r.get("Name"),
                "x_um": float(_require(r, "X").text),
                "y_um": float(_require(r, "Y").text),
                "z": float(_require(r, "Z").text),
            }
            for r in regions
        ],
        "zstack_first_m": zval("First"),
        "zstack_last_m": zval("Last"),
        "zstack_interval_m": zval("Interval"),
        "zoom": _parse_pair(_require(det, "Zoom").text),
        "frame_size": _parse_pair(_require(det, "FrameSize").text),
        "sampling": float(_require(det, "Sampling").text),
        "scaled_rect_um": _parse_pair(_require(det, "ScaledImageRectangleSize").text),
        "scaled_max_um": _parse_pair(_require(det, "ScaledMaxImageSize").text),
        "pixel_um": get_lsm_pixel_size_um(root),
    }
    return out


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def set_position(root, x_m, y_m, z_m=None, region_index=0):
    """Set a tile region's stage position.  X/Y/Z are written in µm (× 1e6).

    The tile-region <Z> is the focus reference: with a CENTER-mode z-stack ZEN
    centres the stack on this value, so it MUST be the target centre Z (not the
    base template's placeholder).
    """
    regions = find_tile_regions(root)
    if not regions:
        raise ValueError("no SingleTileRegion to set position on")
    r = regions[region_index]
    _require(r, "X").text = _fmt_float(x_m * 1e6)
    _require(r, "Y").text = _fmt_float(y_m * 1e6)
    if z_m is not None:
        _require(r, "Z").text = _fmt_float(z_m * 1e6)
    return x_m * 1e6, y_m * 1e6, (z_m * 1e6 if z_m is not None else None)


def add_single_tile_region(root, x_m, y_m, z_m=None, name="P1",
                           region_id="639171104957762277"):
    """Ensure a positioned <SingleTileRegion> exists; set its X/Y/Z (µm).

    For an experiment whose <SingleTileRegions> container is empty (e.g. a FAZ
    z-stack authored without a position), this CREATES the region so the
    experiment can be positioned in holder/sample coordinates and run by path —
    ZEN then applies the carrier calibration, exactly like the OV/DV (a raw
    StageService move would be in machine coords and miss on a calibrated plate).
    If a SingleTileRegion already exists it is updated in place.

    With a CENTER-mode z-stack the <Z> is the stack centre, so pass the target
    centre Z (e.g. the overview focus); leave z_m=None to write 0.
    """
    existing = list(root.iter("SingleTileRegion"))
    if existing:
        return set_position(root, x_m, y_m, z_m=z_m)
    containers = list(root.iter("SingleTileRegions"))
    if not containers:
        raise ValueError("no <SingleTileRegions> container to add a region to")
    st = ET.SubElement(containers[0], "SingleTileRegion")
    st.set("Name", name)
    st.set("Id", str(region_id))
    ET.SubElement(st, "X").text = _fmt_float(x_m * 1e6)
    ET.SubElement(st, "Y").text = _fmt_float(y_m * 1e6)
    ET.SubElement(st, "Z").text = _fmt_float((z_m if z_m is not None else 0.0) * 1e6)
    ET.SubElement(st, "IsUsedForAcquisition").text = "true"
    ET.SubElement(st, "AdditionalValues")
    return x_m * 1e6, y_m * 1e6, (z_m * 1e6 if z_m is not None else None)


def set_tile_region_center(root, x_m, y_m, z_m=None, region_index=0):
    """Set a multi-tile (stitch) TileRegion's CenterPosition (written in µm).

    Used to re-aim a stitch experiment (e.g. the OV) at a new well centre.
    Optionally sets the region <Z>; leave z_m=None if the experiment runs its
    own autofocus.
    """
    regions = find_stitch_regions(root)
    if not regions:
        raise ValueError("no TileRegion (stitch region) to set centre on")
    r = regions[region_index]
    _require(r, "CenterPosition").text = f"{_fmt_float(x_m * 1e6)},{_fmt_float(y_m * 1e6)}"
    if z_m is not None:
        z_el = r.find("Z")
        if z_el is not None:
            z_el.text = _fmt_float(z_m * 1e6)
    return x_m * 1e6, y_m * 1e6


def clear_single_tile_regions(root):
    """Remove standalone single-tile regions, leaving only <TileRegion> stitches.

    A stitch experiment (e.g. the OV) may also carry stray <SingleTileRegion> /
    <SingleTileRegionArray> entries.  ZEN acquires each as an EXTRA scene, so a
    re-aimed OV would image both the intended well AND those leftover positions,
    producing a multi-scene mosaic that breaks single-well analysis.  Removing
    them keeps the OV to the one re-aimed TileRegion.  Returns the count removed.
    """
    removed = 0
    for container_tag in ("SingleTileRegions", "SingleTileRegionArrays"):
        for container in root.iter(container_tag):
            for child in list(container):
                container.remove(child)
                removed += 1
    return removed


def set_zstack_range(root, first_m, last_m, interval_m=None):
    """Set the z-stack First/Last (metres); optionally the Interval (step)."""
    zs = find_zstack_setup(root)

    def set_val(tag, value_m):
        _require(_require(zs, tag), "Distance").find("Value").text = _fmt_float(value_m)

    set_val("First", first_m)
    set_val("Last", last_m)
    if interval_m is not None:
        set_val("Interval", interval_m)
    return first_m, last_m


def fit_lsm_crop(root, target_fov_um, frame_size_override=None):
    """Resize the LSM scan field to target_fov_um (set via Zoom).

    Zoom sets the field of view; the frame size sets the sampling within it:
      frame_size_override=None → constant pixel size (frame = fov / base pixel,
                                  Nyquist as in the base experiment).
      frame_size_override=N    → fixed N×N scan frame (pixel = fov / N), e.g. a
                                  fast 512² preview that is NOT Nyquist-sampled.

    Sets Zoom, FrameSize and the dependent FOV / Frame / ImageFrame fields,
    re-centred.  Returns a dict of applied values.

    Raises:
        ValueError: If ``target_fov_um`` is not positive (e.g. a degenerate
            size from upstream analysis) — the FOV is a divisor below, so a
            zero/negative value would otherwise crash with ZeroDivisionError.
    """
    if target_fov_um <= 0:
        raise ValueError(f"target_fov_um must be positive, got {target_fov_um}")

    det = find_lsm_detector(root)
    base_frame_x, _ = _parse_pair(_require(det, "FrameSize").text)
    base_rect_x, _ = _parse_pair(_require(det, "ScaledImageRectangleSize").text)
    base_zoom_x, _ = _parse_pair(_require(det, "Zoom").text)
    scaled_max_x, _ = _parse_pair(_require(det, "ScaledMaxImageSize").text)
    base_sampling = float(_require(det, "Sampling").text)

    base_pixel_um = base_rect_x / base_frame_x

    # Nyquist frame for this FOV at the base sampling (constant pixel size).
    nyquist_frame = target_fov_um / base_pixel_um

    if frame_size_override is not None:
        new_frame = int(frame_size_override)
    else:
        new_frame = int(round(nyquist_frame))
    new_frame = max(FRAME_SIZE_MIN, min(FRAME_SIZE_MAX, new_frame))

    # In ZEN the frame size is DERIVED from FOV × Sampling and is re-computed on
    # load — so to make a frame size stick we must set Sampling to match it.
    # (frame ∝ sampling at fixed FOV; constant-pixel mode leaves it ≈ base.)
    new_sampling = base_sampling * new_frame / nyquist_frame

    # Zoom sets the FOV (independent of frame); pixel size follows from frame.
    # Self-calibrated zoom: preserve (zoom × FOV) from the base file.
    zoom_const = base_zoom_x * base_rect_x
    new_zoom = zoom_const / target_fov_um
    pixel_um = target_fov_um / new_frame

    _require(det, "FrameSize").text = f"{new_frame},{new_frame}"
    _require(det, "Zoom").text = f"{_fmt_float(new_zoom)},{_fmt_float(new_zoom)}"
    _require(det, "Sampling").text = _fmt_float(new_sampling)

    # SamplingMode is the master control: "SR" makes ZEN auto-derive the frame
    # (Nyquist) and ignore our FrameSize; "User" honours the manual frame.
    # Only switch to User when we force a frame size (e.g. fast 512² test).
    sampling_mode = None
    if frame_size_override is not None:
        sm = det.find("SamplingMode")
        if sm is not None:
            sm.text = "User"
            sampling_mode = "User"

    # Derived FOV fields (µm), re-centred within the zoom-independent max field.
    rect_off = (scaled_max_x - target_fov_um) / 2.0
    _require(det, "ScaledImageRectangleSize").text = (
        f"{_fmt_float(target_fov_um)},{_fmt_float(target_fov_um)}")
    rect = det.find("ScaledImageRectangle")
    if rect is not None:
        rect.text = (f"{_fmt_float(rect_off)},{_fmt_float(rect_off)},"
                     f"{_fmt_float(target_fov_um)},{_fmt_float(target_fov_um)}")

    # Frame / ImageFrame = the px scan ROI centred in the full scan field.
    # Full field in px = ScaledMaxImageSize / pixel_um; centred offset follows.
    # (Matches ZEN: e.g. (397.96/0.405 − 512)/2 ≈ 235 for a 512² @ 207.5 µm FOV.)
    frame_off = (scaled_max_x / pixel_um - new_frame) / 2.0
    for tag in ("Frame", "ImageFrame"):
        el = det.find(tag)
        if el is not None:
            el.text = (f"{_fmt_float(frame_off)},{_fmt_float(frame_off)},"
                       f"{new_frame},{new_frame}")

    return {
        "base_pixel_um": base_pixel_um,
        "pixel_um": pixel_um,
        "target_fov_um": target_fov_um,
        "actual_fov_um": target_fov_um,
        "frame_size": new_frame,
        "zoom": new_zoom,
        "sampling": new_sampling,
        "sampling_mode": sampling_mode,
    }


def set_run_mode_lock(root):
    """Remove perform-time optimize/validate flags from <RunMode>.

    The base experiment runs with OptimizeBeforePerformEnabled and
    ValidateAndAdaptBeforePerformEnabled, which make ZEN re-derive acquisition
    settings (notably the frame size) at run time — overriding our manual
    FrameSize/Sampling.  Stripping them makes ZEN run the experiment as authored.
    Returns the new RunMode string (or None if absent).
    """
    rm = root.find("RunMode")
    if rm is None or not rm.text:
        return None
    drop = {"OptimizeBeforePerformEnabled", "ValidateAndAdaptBeforePerformEnabled"}
    keep = [f.strip() for f in rm.text.split(",") if f.strip() and f.strip() not in drop]
    rm.text = ",".join(keep)
    return rm.text


def set_lsm_scan_speed_max(root):
    """Use the maximum LSM scan speed (fastest scan, lowest frame time).

    Sets IsMaxScanSpeedSet=true so ZEN clamps to the hardware max on load.
    """
    det = find_lsm_detector(root)
    el = det.find("IsMaxScanSpeedSet")
    if el is not None:
        el.text = "true"
        return True
    return False


def set_lsm_sampling_mode_user(root):
    """Force SamplingMode=User on the LSM detector so the authored frame survives.

    With SamplingMode=SR, ZEN auto-derives the XY frame to Nyquist on load/run and
    IGNORES the authored FrameSize.  Switching to "User" makes ZEN honour the frame
    exactly as authored — no automatic XY rescaling.  Returns the frame size string
    that is now locked in (for logging), or None if there is no SamplingMode field.
    """
    det = find_lsm_detector(root)
    sm = det.find("SamplingMode")
    if sm is None:
        return None
    sm.text = "User"
    fr = det.find("FrameSize")
    return fr.text if fr is not None else "User"


# ---------------------------------------------------------------------------
# CLI: inspect a czexp, or dry-run a fit
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python MS_czexp_editor.py <file.czexp>              # summarize")
        print("  python MS_czexp_editor.py <file.czexp> fit <fov_um> # dry-run crop fit")
        sys.exit(1)

    src = Path(sys.argv[1])
    _, root = load_czexp(src)

    if len(sys.argv) >= 4 and sys.argv[2] == "fit":
        fov = float(sys.argv[3])
        print("Before:", json.dumps(summarize(root), indent=2, default=str))
        applied = fit_lsm_crop(root, fov)
        print("Applied:", json.dumps(applied, indent=2, default=str))
        print("After:", json.dumps(summarize(root), indent=2, default=str))
    else:
        print(json.dumps(summarize(root), indent=2, default=str))
