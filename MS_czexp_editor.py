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

Pure stdlib (xml.etree) plus ``MS_limits`` — no ZEN API, no extra dependency.  Generic helpers for
any project that needs to customise an experiment per target (position, z-stack,
scan crop) and then run the result via the LoA's run_experiment_from_path.

A .czexp is a ZEN HardwareExperiment XML.  The fields this module edits:

  POSITION   SingleTileRegion "P1"  → <X> <Y> <Z>          (stage coords in µm)
  Z-STACK    ZStackSetup            → <First>/<Last>/<Interval> <Value>
                                       (values are in METRES despite the "um" label;
                                       relative or absolute per <FocusZStackMode>)
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
  * <FocusZStackMode> (in the focus strategy), NOT <IsCenterMode>, decides how
    ZEN reads the z-stack First/Last (Marc_SM_003, 2026-11-02):
      CurrentZ → First/Last are OFFSETS from the focus at the position
                 (the tile-region <Z> when there is a used region);
      FixedZ   → First/Last are ABSOLUTE Z, also in CENTER mode; the region
                 <Z> does not shift the stack.
    Read it with get_focus_z_stack_mode; pass expect_mode= to set_zstack_range
    so a relative range is never written into a FixedZ file (that ran ~2.9 mm
    below the sample) or vice versa.
  * Frame size is DERIVED from FOV × Sampling and is recomputed on load under
    SamplingMode=SR; forcing a manual frame requires SamplingMode=User + a
    matching Sampling value (this is how the FAZ experiment keeps its 512²).
  * Tiles switched on with no tile region: ZEN refuses to start the run
    ("Failed to start experiment", Marc_SM_003) — require_runnable_tiles.
    A focus strategy can run a search (DF/SWAF) even if the caller sets Z
    itself — require_no_focus_search.
  * Generated files are run via ExperimentService.Load BY PATH (see the LoA's
    run_experiment_from_path) — load, do NOT import-then-run.

Always give a generated file a unique name (ZEN caches by name on re-import) and
verify it once on the scope before trusting a batch run.

CLI:
    python MS_czexp_editor.py <file.czexp>              # summarize
    python MS_czexp_editor.py <file.czexp> fit <fov_um> # dry-run crop fit
"""

import codecs
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import MS_limits

PathLike = Union[str, Path]
XYZ = Tuple[float, float, float]

LSM_DETECTOR_ID = "MTBLSMImagingDevice"

# LSM frame-size hardware limits (from FrameSizeMin/Max in the base czexp).
FRAME_SIZE_MIN = 32
FRAME_SIZE_MAX = 5120

# FocusStrategy field values that mean "no strategy is doing anything".
FOCUS_OFF_VALUES = {"", "None", "NONE", "none"}

# <FocusZStackMode> values → how ZEN reads the z-stack First/Last.
# Anything else (or a missing field) is unknown: refuse rather than guess.
FOCUS_Z_STACK_MODES = {
    "CurrentZ": "relative",   # offsets from the focus at the position
    "FixedZ": "absolute",     # absolute Z, regardless of IsCenterMode
}


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_czexp(path: PathLike) -> Tuple[ET.ElementTree, ET.Element]:
    """Parse a .czexp file and return (tree, root)."""
    path = Path(path)
    tree = ET.parse(path)
    return tree, tree.getroot()


def save_czexp(tree: ET.ElementTree, path: PathLike) -> Path:
    """Write a .czexp file as UTF-8 with BOM + XML declaration (ZEN style).

    The file is written to ``<name>.tmp`` and then moved into place, so a crash
    during the write never leaves half an experiment under the final name.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    xml_bytes = ET.tostring(tree.getroot(), encoding="utf-8", xml_declaration=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(codecs.BOM_UTF8)
        f.write(xml_bytes)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Small parsing/formatting helpers
# ---------------------------------------------------------------------------

def _fmt_float(v: float) -> str:
    """Shortest round-trippable float string (e.g. 0.00111297)."""
    return repr(float(v))


def _parse_pair(text: str) -> Tuple[float, float]:
    """'1859,1859' -> (1859.0, 1859.0)."""
    a, b = text.split(",")[:2]
    return float(a), float(b)


def _require(elem: ET.Element, tag: str) -> ET.Element:
    child = elem.find(tag)
    if child is None:
        raise ValueError(f"<{tag}> not found under <{elem.tag}>")
    return child


# ---------------------------------------------------------------------------
# Element locators
# ---------------------------------------------------------------------------

def _iter_own(root: ET.Element, tag: str) -> List[ET.Element]:
    """root.iter(tag), minus elements inside an embedded <Experiment>.

    A software-autofocus strategy carries its own complete experiment
    (FocusSetup/SoftwareAutofocus/Extensions/Extension/Experiment/...) with its
    own FocusSetup and ZStackSetup; those describe the autofocus run, not this
    experiment's acquisition.
    """
    embedded = set()
    for exp in root.iter("Experiment"):
        if exp is not root:
            embedded.update(exp.iter(tag))
    return [e for e in root.iter(tag) if e not in embedded]


def find_lsm_detector(root: ET.Element) -> ET.Element:
    """Return the experiment's own <Detector Id='MTBLSMImagingDevice'> element.

    All locators in this section ignore the copy inside a software-autofocus
    sub-experiment (see _iter_own).
    """
    for det in _iter_own(root, "Detector"):
        if det.get("Id") == LSM_DETECTOR_ID:
            return det
    raise ValueError(f"LSM detector '{LSM_DETECTOR_ID}' not found")


def find_tile_regions(root: ET.Element) -> List[ET.Element]:
    """Return all <SingleTileRegion> elements (single-position acquisitions)."""
    return _iter_own(root, "SingleTileRegion")


def find_stitch_regions(root: ET.Element) -> List[ET.Element]:
    """Return all <TileRegion> elements (multi-tile / stitch regions).

    A stitch region is positioned by its <CenterPosition> (µm) and sized by
    <ContourSize> / <Columns> / <Rows>, unlike a SingleTileRegion (<X>/<Y>).
    """
    return _iter_own(root, "TileRegion")


def find_zstack_setup(root: ET.Element) -> ET.Element:
    """Return the <ZStackSetup> element that defines First/Last/Interval.

    A software-autofocus sub-experiment's own ZStackSetup is never returned.
    """
    for zs in _iter_own(root, "ZStackSetup"):
        if zs.find("First") is not None and zs.find("Last") is not None:
            return zs
    raise ValueError("ZStackSetup with First/Last not found")


def find_processing_steps(root: ET.Element) -> List[ET.Element]:
    """Return all <ProcessingStep> elements (remote-processing / stitching steps)."""
    return _iter_own(root, "ProcessingStep")


def is_stitching_configured(root: ET.Element) -> bool:
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
    setup = next(iter(_iter_own(root, "ExperimentRemoteProcessingSetup")), None)
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


def get_focus_strategy(root: ET.Element) -> Optional[Dict[str, str]]:
    """Return the active focus strategy as a dict, or None if there is none.

    The strategy lives in <FocusSetup><FocusStrategy> as <Method>, <SearchAction>
    and <SurfaceMode> (e.g. Method=FollowAction, SearchAction=DefiniteFocusFindSurface).
    Returns {"method", "search_action", "surface_mode"} when a real strategy is
    configured, else None.  See has_focus_strategy for the "what counts as a
    strategy" rules.
    """
    setup = next(iter(_iter_own(root, "FocusSetup")), None)
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


def has_focus_strategy(root: ET.Element) -> bool:
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
# Run-safety checks (raise before a generated .czexp is run)
# ---------------------------------------------------------------------------

def require_no_focus_search(root: ET.Element, path: Optional[PathLike] = None,
                            reason: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Raise if the experiment's focus strategy runs a focus SEARCH.

    For runs that set Z themselves (e.g. Marc_SM_003: DefiniteFocus and SWAF
    don't work on U-bottom plates; focus comes from image sweeps).  A strategy
    WITHOUT a search action is allowed: e.g. Method=FollowSurface with
    SearchAction=None only uses the stored Z of each region, which is how a
    re-aimed OV gets its focus Z.  Stricter than "no strategy at all" — use
    has_focus_strategy for that.

    reason: optional text appended to the error (why this run can't search).
    Returns the strategy dict (see get_focus_strategy) or None.

    Raises:
        ValueError: An active strategy names a SearchAction (DefiniteFocus,
            SoftwareAutofocus, …).
    """
    strategy = get_focus_strategy(root)
    if strategy is not None and strategy["search_action"] not in FOCUS_OFF_VALUES:
        where = f"{path}: " if path is not None else ""
        raise ValueError(
            f"{where}the focus strategy runs a focus search {strategy}; this run sets Z "
            f"itself" + (f" ({reason})" if reason else "")
            + " — remove the search in ZEN and re-save.")
    return strategy


def tiles_activated(root: ET.Element) -> bool:
    """True if the experiment's own <TilesSetup> is switched on (Tiles in ZEN).

    A software-autofocus sub-experiment's TilesSetup is ignored.
    """
    tiles = next(iter(_iter_own(root, "TilesSetup")), None)
    return tiles is not None and (tiles.get("IsActivated") or "false").lower() == "true"


def require_runnable_tiles(root: ET.Element, path: Optional[PathLike] = None) -> None:
    """Raise if Tiles is switched on but the experiment has no tile region.

    ZEN refuses to start such an experiment ("ABORTED: Failed to start
    experiment", Marc_SM_003 shakedown 2026-11-02: a final LSM base saved with
    Tiles on and no region).  With Tiles OFF the experiment acquires at the
    current stage position — the usual set-up for a base the caller positions
    by moving the stage first.  Any <TileRegion> or <SingleTileRegion> counts
    (IsUsedForAcquisition is not checked).  Regions inside a software-autofocus
    sub-experiment are ignored.

    Raises:
        ValueError: Tiles on, no region.
    """
    if not tiles_activated(root):
        return
    if find_stitch_regions(root) or find_tile_regions(root):
        return
    where = f"{path}: " if path is not None else ""
    raise ValueError(
        f"{where}Tiles is switched on but there is no tile region — ZEN refuses to start "
        f"it (\"Failed to start experiment\"). Switch Tiles off in ZEN (the experiment "
        f"then acquires at the stage position) or add a region, and re-save.")


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def get_lsm_pixel_size_um(root: ET.Element) -> float:
    """Current LSM pixel size in µm = ScaledImageRectangleSize / FrameSize."""
    det = find_lsm_detector(root)
    rect_x, _ = _parse_pair(_require(det, "ScaledImageRectangleSize").text)
    frame_x, _ = _parse_pair(_require(det, "FrameSize").text)
    return rect_x / frame_x


def get_zstack_interval_m(root: ET.Element) -> float:
    """Current z-stack interval (step) in metres."""
    zs = find_zstack_setup(root)
    return float(_require(_require(zs, "Interval"), "Distance").find("Value").text)


def get_focus_z_stack_mode(root: ET.Element) -> Optional[str]:
    """The experiment's <FocusZStackMode>: "CurrentZ", "FixedZ", or None.

    THIS decides how ZEN reads the z-stack First/Last, not <IsCenterMode>
    (found in Marc_SM_003, 2026-11-02; DEV_NOTES "FocusZStackMode, not
    IsCenterMode"):
      CurrentZ → First/Last are offsets from the focus at the position.
      FixedZ   → First/Last are absolute Z, also when IsCenterMode is true.
                 A FixedZ + CENTER-mode stack given relative ±200 µm ran at
                 absolute Z −200 … +200 µm.
    The value is returned as written; check it against FOCUS_Z_STACK_MODES
    (or use require_focus_z_stack_mode) before relying on it.

    The copy inside a software-autofocus sub-experiment is ignored.  Returns
    None when the field is absent (no focus strategy saved).

    Raises:
        ValueError: The experiment's own acquisition blocks disagree.
    """
    modes = {e.text.strip() for e in _iter_own(root, "FocusZStackMode")
             if e.text and e.text.strip()}
    if len(modes) > 1:
        raise ValueError(f"conflicting FocusZStackMode values {sorted(modes)}")
    return modes.pop() if modes else None


def require_focus_z_stack_mode(root: ET.Element, expected: str,
                               path: Optional[PathLike] = None) -> str:
    """Raise unless the experiment's FocusZStackMode is ``expected``.

    expected: "CurrentZ" (First/Last relative) or "FixedZ" (absolute).
    Returns the mode.  Use before writing a z-stack range, so that offsets are
    never written into a FixedZ file (or absolute Z into a CurrentZ file).

    Raises:
        ValueError: ``expected`` is not a known mode, or the file's mode is
            different, missing or unknown.
    """
    if expected not in FOCUS_Z_STACK_MODES:
        raise ValueError(f"expected must be one of {sorted(FOCUS_Z_STACK_MODES)}, "
                         f"got {expected!r}")
    found = get_focus_z_stack_mode(root)
    if found != expected:
        where = f"{path}: " if path is not None else ""
        raise ValueError(
            f"{where}FocusZStackMode is {found!r}, expected {expected!r} "
            f"({FOCUS_Z_STACK_MODES[expected]} z-stack First/Last). It decides "
            f"whether ZEN reads First/Last as offsets (CurrentZ) or absolute Z "
            f"(FixedZ) — change it in ZEN and re-save, or write the other kind of range.")
    return found


def is_zstack_center_mode(root: ET.Element) -> bool:
    """True if the z-stack is in CENTER mode (<IsCenterMode>true</IsCenterMode>).

    Only says how the stack is set up in the ZEN UI (centre ± range vs
    first/last); whether First/Last are relative or absolute is decided by
    get_focus_z_stack_mode.
    """
    flag = find_zstack_setup(root).find("IsCenterMode")
    return flag is not None and (flag.text or "").strip().lower() == "true"


def _used_stitch_regions(root: ET.Element) -> List[Tuple[str, ET.Element]]:
    """[(name, <TileRegion>)] of the multi-tile regions used for acquisition.

    File order. IsUsedForAcquisition=false is skipped; a missing flag counts as
    used (as in load_positions_from_czexp).

    Raises:
        ValueError: A used region without a Name, or two with the same Name.
    """
    used = {}
    for tr in find_stitch_regions(root):
        if (tr.findtext("IsUsedForAcquisition") or "true").strip().lower() != "true":
            continue
        name = tr.get("Name")
        if not name:
            raise ValueError("<TileRegion> without a Name attribute")
        if name in used:
            raise ValueError(f"two used <TileRegion>s are named {name!r}")
        used[name] = tr
    return list(used.items())


def get_tile_region_centers(root: ET.Element) -> Dict[str, Tuple[float, float]]:
    """{region name: (x_m, y_m)} of the multi-tile regions used for acquisition.

    Reads each <TileRegion>'s <CenterPosition> (µm in the file) and returns
    metres, in file order. Regions with IsUsedForAcquisition=false are skipped;
    a missing flag counts as used (as in load_positions_from_czexp). E.g. an OV
    experiment with one tile region per well → the well centres.

    Raises:
        ValueError: A used region without a Name or CenterPosition, or two used
            regions with the same Name.
    """
    centers = {}
    for name, tr in _used_stitch_regions(root):
        x_um, y_um = _parse_pair(_require(tr, "CenterPosition").text)
        centers[name] = (x_um * 1e-6, y_um * 1e-6)
    return centers


def get_tile_region_shape_ids(root: ET.Element) -> Dict[str, str]:
    """{region name: TemplateShapeId} of the multi-tile regions used for acquisition.

    <TemplateShapeId> is ZEN's link from a region to its sample-carrier
    container (well): "row-column", both 1-based, e.g. B2 → "2-2", H12 →
    "8-12" (seen on 96- and 384-well carriers). Read it from an experiment
    ZEN laid out with a region per well (e.g. a whole-plate OV) instead of
    working it out from the well name: the name is only a label, so this
    stays right for any naming (rows past Z on a 1536-well plate) and for
    carriers whose row/column labels are switched or inverted.

    Same region selection as get_tile_region_centers (used regions, file
    order). A region without a TemplateShapeId maps to None.

    Raises:
        ValueError: A used region without a Name, or two with the same Name.
    """
    ids = {}
    for name, tr in _used_stitch_regions(root):
        text = tr.findtext("TemplateShapeId")
        ids[name] = text.strip() if text and text.strip() else None
    return ids


def summarize(root: ET.Element) -> Dict[str, Any]:
    """Return a dict of the current values this module can edit (for logging)."""
    det = find_lsm_detector(root)
    zs = find_zstack_setup(root)
    regions = find_tile_regions(root)

    def zval(tag: str) -> float:
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
        "focus_z_stack_mode": get_focus_z_stack_mode(root),
        "zstack_center_mode": is_zstack_center_mode(root),
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

def _require_position(x_m: float, y_m: float, z_m: Optional[float]) -> None:
    """Raise ValueError unless X/Y (and Z, if given) are finite and in range.

    ZEN drives the stage to what is in the file, so a position gets the same
    check here as a direct move (``MS_limits``). Call it before the first write.
    """
    MS_limits.require_xy_position(x_m, y_m)
    if z_m is not None:
        MS_limits.require_z_position(z_m)


def set_position(root: ET.Element, x_m: float, y_m: float, z_m: Optional[float] = None,
                 region_index: int = 0) -> Tuple[float, float, Optional[float]]:
    """Set a tile region's stage position.  X/Y/Z are written in µm (× 1e6).

    What the region <Z> does to a z-stack depends on <FocusZStackMode>
    (get_focus_z_stack_mode), not on CENTER mode:
      CurrentZ → First/Last are offsets from the focus at the position, so
                 <Z> must be the target centre Z (not the base template's
                 placeholder);
      FixedZ   → First/Last are absolute Z; <Z> does not move the stack —
                 write the absolute range with set_zstack_range.

    Raises:
        ValueError: No region, or a position that is not finite or not within
            the limits of ``MS_limits`` (nothing is written then).
    """
    _require_position(x_m, y_m, z_m)
    regions = find_tile_regions(root)
    if not regions:
        raise ValueError("no SingleTileRegion to set position on")
    r = regions[region_index]
    _require(r, "X").text = _fmt_float(x_m * 1e6)
    _require(r, "Y").text = _fmt_float(y_m * 1e6)
    if z_m is not None:
        _require(r, "Z").text = _fmt_float(z_m * 1e6)
    return x_m * 1e6, y_m * 1e6, (z_m * 1e6 if z_m is not None else None)


def add_single_tile_region(root: ET.Element, x_m: float, y_m: float,
                           z_m: Optional[float] = None, name: str = "P1",
                           region_id: str = "639171104957762277",
                           ) -> Tuple[float, float, Optional[float]]:
    """Ensure a positioned <SingleTileRegion> exists; set its X/Y/Z (µm).

    For an experiment whose <SingleTileRegions> container is empty (e.g. a FAZ
    z-stack authored without a position), this CREATES the region so the
    experiment can be positioned in holder/sample coordinates and run by path —
    ZEN then applies the carrier calibration, exactly like the OV/DV (a raw
    StageService move would be in machine coords and miss on a calibrated plate).
    If a SingleTileRegion already exists it is updated in place.

    With FocusZStackMode=CurrentZ the <Z> is the reference the relative
    First/Last are added to, so pass the target centre Z (e.g. the overview
    focus); with FixedZ the stack is absolute and <Z> does not move it (see
    set_position).  Leave z_m=None to write 0.

    Raises:
        ValueError: No container, or a position that is not finite or not
            within the limits of ``MS_limits`` (nothing is written then).
    """
    _require_position(x_m, y_m, z_m)
    if find_tile_regions(root):
        return set_position(root, x_m, y_m, z_m=z_m)
    containers = _iter_own(root, "SingleTileRegions")
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


def set_tile_region_center(root: ET.Element, x_m: float, y_m: float,
                           z_m: Optional[float] = None,
                           region_index: int = 0) -> Tuple[float, float]:
    """Set a multi-tile (stitch) TileRegion's CenterPosition (written in µm).

    Used to re-aim a stitch experiment (e.g. the OV) at a new well centre.
    Optionally sets the region <Z>; leave z_m=None if the experiment runs its
    own autofocus.

    Raises:
        ValueError: No stitch region, no <CenterPosition>, z_m is given and the
            region has no <Z>, or a position that is not finite or not within
            the limits of ``MS_limits`` (nothing is written then).
    """
    _require_position(x_m, y_m, z_m)
    regions = find_stitch_regions(root)
    if not regions:
        raise ValueError("no TileRegion (stitch region) to set centre on")
    r = regions[region_index]
    center = _require(r, "CenterPosition")
    z_el = _require(r, "Z") if z_m is not None else None
    center.text = f"{_fmt_float(x_m * 1e6)},{_fmt_float(y_m * 1e6)}"
    if z_el is not None:
        z_el.text = _fmt_float(z_m * 1e6)
    return x_m * 1e6, y_m * 1e6


def retarget_tile_region(root: ET.Element, name: str, x_m: float, y_m: float,
                         z_m: Optional[float] = None, shape_id: Optional[str] = None,
                         region_index: int = 0) -> Dict[str, Optional[str]]:
    """Re-aim a tile region authored for one well at another well.

    A region is tied to its well three ways, and all three are rewritten:
      * <CenterPosition> (and <Z> if z_m is given), via set_tile_region_center;
      * its Name, which becomes the CZI scene name;
      * its <TemplateShapeId> (the container link, "row-column"), when the
        region has one. Take shape_id from get_tile_region_shape_ids on an
        experiment with a region per well, not from the well name.
    The tiling (columns, rows, contour) is left as authored.

    Why the container link too (Marc_SM_003, 2026-11-02): with ZEN's container
    adaptation on (<IsContainerAdaptionEnabled>true), a region still linked
    to the authored well might be moved back there by ZEN. Not observed —
    the re-aimed regions were always rewritten — but a stale link is refused
    rather than risked.

    Returns {"authored_name", "authored_shape_id"} (the values replaced).

    Raises:
        ValueError: The region has a TemplateShapeId and shape_id is None, or
            shape_id is not "row-column" digits.
    """
    regions = find_stitch_regions(root)
    if not regions:
        raise ValueError("no TileRegion (stitch region) to re-aim")
    region = regions[region_index]
    shape = region.find("TemplateShapeId")
    if shape_id is not None and not re.fullmatch(r"\d+-\d+", str(shape_id)):
        raise ValueError(f"TemplateShapeId must be 'row-column' (1-based), got {shape_id!r}")
    if shape is not None and shape_id is None:
        raise ValueError(
            f"region {region.get('Name')!r} is linked to container {shape.text!r}; pass the "
            f"target's shape_id (get_tile_region_shape_ids) so the link moves with it")
    authored = {"authored_name": region.get("Name"),
                "authored_shape_id": shape.text if shape is not None else None}
    set_tile_region_center(root, x_m, y_m, z_m=z_m, region_index=region_index)
    region.set("Name", name)
    if shape is not None:
        shape.text = str(shape_id)
    return authored


def clear_single_tile_regions(root: ET.Element) -> int:
    """Remove standalone single-tile regions, leaving only <TileRegion> stitches.

    A stitch experiment (e.g. the OV) may also carry stray <SingleTileRegion> /
    <SingleTileRegionArray> entries.  ZEN acquires each as an EXTRA scene, so a
    re-aimed OV would image both the intended well AND those leftover positions,
    producing a multi-scene mosaic that breaks single-well analysis.  Removing
    them keeps the OV to the one re-aimed TileRegion.  Returns the count removed.
    """
    removed = 0
    for container_tag in ("SingleTileRegions", "SingleTileRegionArrays"):
        for container in _iter_own(root, container_tag):
            for child in list(container):
                container.remove(child)
                removed += 1
    return removed


def set_zstack_range(root: ET.Element, first_m: float, last_m: float,
                     interval_m: Optional[float] = None,
                     expect_mode: Optional[str] = None) -> Tuple[float, float]:
    """Set the z-stack First/Last (metres); optionally the Interval (step).

    How ZEN reads First/Last is decided by the file's <FocusZStackMode>
    (get_focus_z_stack_mode), NOT by <IsCenterMode>:
      "CurrentZ" → offsets from the focus at the position (e.g. −h … +h);
      "FixedZ"   → absolute Z (e.g. z0 − h … z0 + h), also in CENTER mode.
    Pass ``expect_mode`` ("CurrentZ" or "FixedZ") for the kind of range you
    are writing; a file in the other (or no/unknown) mode is refused before
    anything is written.  ``expect_mode=None`` writes without checking — kept
    for existing callers; new code should always pass it.

    Raises:
        ValueError: ``expect_mode`` is given and the file does not match it.
    """
    if expect_mode is not None:
        require_focus_z_stack_mode(root, expect_mode)
    zs = find_zstack_setup(root)

    def set_val(tag: str, value_m: float) -> None:
        _require(_require(zs, tag), "Distance").find("Value").text = _fmt_float(value_m)

    set_val("First", first_m)
    set_val("Last", last_m)
    if interval_m is not None:
        set_val("Interval", interval_m)
    return first_m, last_m


def fit_lsm_crop(root: ET.Element, target_fov_um: float,
                 frame_size_override: Optional[int] = None) -> Dict[str, Any]:
    """Resize the LSM scan field to target_fov_um (set via Zoom).

    Zoom sets the field of view; the frame size sets the sampling within it:
      frame_size_override=None → constant pixel size (frame = fov / base pixel,
                                  Nyquist as in the base experiment).
      frame_size_override=N    → fixed N×N scan frame (pixel = fov / N), e.g. a
                                  fast 512² preview that is NOT Nyquist-sampled.

    Sets Zoom, FrameSize and the dependent FOV / Frame / ImageFrame fields,
    re-centred.  Returns a dict of applied values. Two keys flag a result that
    is not what was asked for (nothing is refused; check them):
      frame_clamped   → the frame was limited to FRAME_SIZE_MIN…FRAME_SIZE_MAX
                        (frame_size_requested is the value before the limit);
      fov_exceeds_max → target_fov_um is larger than ScaledMaxImageSize, the
                        largest field the scanner has. What ZEN then does is
                        not known: do not trust actual_fov_um.

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
        requested_frame = int(frame_size_override)
    else:
        requested_frame = int(round(nyquist_frame))
    new_frame = max(FRAME_SIZE_MIN, min(FRAME_SIZE_MAX, requested_frame))

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
        "frame_size_requested": requested_frame,
        "frame_clamped": new_frame != requested_frame,
        "fov_exceeds_max": target_fov_um > scaled_max_x,
        "zoom": new_zoom,
        "sampling": new_sampling,
        "sampling_mode": sampling_mode,
    }


def set_run_mode_lock(root: ET.Element) -> Optional[str]:
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


def set_lsm_scan_speed_max(root: ET.Element) -> bool:
    """Use the maximum LSM scan speed (fastest scan, lowest frame time).

    Sets IsMaxScanSpeedSet=true so ZEN clamps to the hardware max on load.
    """
    det = find_lsm_detector(root)
    el = det.find("IsMaxScanSpeedSet")
    if el is not None:
        el.text = "true"
        return True
    return False


def set_lsm_sampling_mode_user(root: ET.Element) -> Optional[str]:
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
# Builder: an LSM z-stack fitted to one object
# ---------------------------------------------------------------------------

def fit_lsm_stack_to_box(base_path: PathLike, out_path: PathLike, center_m: XYZ,
                         size_m: XYZ, *, expect_mode: str,
                         xy_margin_m: float, z_margin_m: float,
                         z_step_m: Optional[float] = None,
                         frame_size: Optional[int] = None,
                         sampling_mode_user: bool = True, max_scan_speed: bool = False,
                         lock_run_mode: bool = False,
                         forbid_focus_search: Union[bool, str] = False) -> Dict[str, Any]:
    """Write a copy of an LSM z-stack experiment that images one object's box.

    center_m = (x, y, z), size_m = (sx, sy, sz): the object's box, metres.
    The copy gets:
      * a z-stack over center z ± (sz/2 + z_margin_m), written the way the
        base's FocusZStackMode says ZEN reads it (offsets for CurrentZ,
        absolute Z for FixedZ);
      * a square LSM field of view of max(sx, sy) + 2·xy_margin_m
        (fit_lsm_crop: constant pixel size, or frame_size×frame_size);
      * the region X/Y/Z set to the centre, IF the base has a position
        region. A base without one acquires at the current stage position:
        the caller moves there first (see "z_reference" below).

    expect_mode: "CurrentZ" or "FixedZ", the mode the caller has written its
        Z handling for. A base in another (or no) mode is refused, so a range
        is never written in the wrong kind of Z (DEV_NOTES "FocusZStackMode,
        not IsCenterMode": relative ±200 µm in a FixedZ file ran at absolute
        −200 … +200 µm).
    sampling_mode_user: set SamplingMode=User so ZEN keeps the fitted frame
        (default; Marc_SM_003, HD_Nuclei). With False, only a forced
        frame_size switches to User (fit_lsm_crop).
    max_scan_speed / lock_run_mode: set_lsm_scan_speed_max / set_run_mode_lock.
    forbid_focus_search: True, or a reason string, refuses a base whose focus
        strategy runs a search (require_no_focus_search). Tiles on without a
        region is always refused (require_runnable_tiles).

    The caller still owns the safety of the Z it asks for (its Z guard /
    ceiling): this only writes the requested range.

    Returns a dict: out_path, focus_z_stack_mode, positioned_by_region,
    z_reference ("absolute" | "region_z" | "current_z": what the z-stack is
    relative to; "current_z" means the caller must move Z to center z before
    the run), first_m / last_m (as written), half_z_m, z_step_m, n_slices,
    max_scan_speed, run_mode, plus fit_lsm_crop's values (actual_fov_um,
    frame_size, zoom, pixel_um, …; sampling_mode as written).

    Raises:
        ValueError: mode mismatch, a refused base (see above), or a size that
            gives no field of view.
    """
    if expect_mode not in FOCUS_Z_STACK_MODES:
        raise ValueError(f"expect_mode must be one of {sorted(FOCUS_Z_STACK_MODES)}, "
                         f"got {expect_mode!r}")
    cx, cy, cz = center_m
    sx, sy, sz = size_m
    tree, root = load_czexp(base_path)
    require_runnable_tiles(root, base_path)
    if forbid_focus_search:
        require_no_focus_search(root, base_path,
                                reason=forbid_focus_search if isinstance(forbid_focus_search, str)
                                else None)
    require_focus_z_stack_mode(root, expect_mode, base_path)

    regions = find_tile_regions(root)
    if regions:
        r = regions[0]
        _require(r, "X").text = _fmt_float(cx * 1e6)
        _require(r, "Y").text = _fmt_float(cy * 1e6)
        _require(r, "Z").text = _fmt_float(cz * 1e6)

    half_z = sz / 2.0 + z_margin_m
    if expect_mode == "CurrentZ":                  # offsets from the focus at the position
        first, last = -half_z, half_z
        z_reference = "region_z" if regions else "current_z"
    else:                                          # FixedZ: absolute Z
        first, last = cz - half_z, cz + half_z
        z_reference = "absolute"
    set_zstack_range(root, first, last, interval_m=z_step_m)

    crop = fit_lsm_crop(root, (max(sx, sy) + 2.0 * xy_margin_m) * 1e6,
                        frame_size_override=frame_size)
    if sampling_mode_user and set_lsm_sampling_mode_user(root) is not None:
        crop["sampling_mode"] = "User"
    scan_max = set_lsm_scan_speed_max(root) if max_scan_speed else False
    run_mode = set_run_mode_lock(root) if lock_run_mode else None
    save_czexp(tree, out_path)

    step = get_zstack_interval_m(root)
    return {"out_path": str(out_path), "focus_z_stack_mode": expect_mode,
            "positioned_by_region": bool(regions), "z_reference": z_reference,
            "first_m": first, "last_m": last, "half_z_m": half_z, "z_step_m": step,
            "n_slices": int(round(2.0 * half_z / step)) + 1,
            "max_scan_speed": scan_max, "run_mode": run_mode, **crop}


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
