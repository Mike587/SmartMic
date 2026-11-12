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
Read and change ZEN ``.czexp`` experiment files.

This module uses the standard library (xml.etree) and ``MS_limits``. It uses no
ZEN API. With it you set an experiment up for one target: the position, the
z-stack and the scan crop. Then you run the experiment with
``run_experiment_from_path`` of the LoA.

The module changes these fields in the HardwareExperiment XML:

  POSITION   SingleTileRegion "P1"  <X> <Y> <Z>, the stage position in µm.
  Z-STACK    ZStackSetup            <First>, <Last> and <Interval> <Value>.
                                    The values are in METRES, although the
                                    label says "um". They are relative or
                                    absolute, as <FocusZStackMode> says.
  CROP       Detector               <Zoom> <FrameSize> <Frame> <ImageFrame>
             "MTBLSMImagingDevice"  <Sampling> <SamplingMode>
                                    <ScaledImageRectangle(Size)>
                                    <ScaledMaxImageSize>
  SCAN SPEED Detector               <IsMaxScanSpeedSet>
             "MTBLSMImagingDevice"
  RUN MODE   HardwareExperiment     <RunMode>

Be careful with ZEN (DEV_NOTES "Positioning an acquisition at a target XY(Z)"):

  * <FocusZStackMode>, NOT <IsCenterMode>, controls how ZEN reads First and
    Last. CurrentZ makes them OFFSETS from the focus at the position, which is
    the <Z> of the region. FixedZ makes them an ABSOLUTE Z, also in CENTER
    mode, and the <Z> of the region does not move the stack. Give
    ``expect_mode`` to ``set_zstack_range``. Then the code never writes a
    relative range into a FixedZ file, or an absolute range into a CurrentZ
    file (DEV_NOTES "FocusZStackMode, not IsCenterMode").
  * ZEN calculates the frame size from the field of view and the sampling. It
    calculates the size again at the load if SamplingMode is SR. A frame size
    that you set needs SamplingMode=User and a sampling value that agrees.
  * If Tiles is on and there is no tile region, ZEN refuses to start the
    experiment. Use ``require_runnable_tiles``. A focus strategy can run a
    search with DF or SWAF, also when the caller sets Z. Use
    ``require_no_focus_search``.
  * Run a file that this module makes BY PATH, with
    ``run_experiment_from_path``. Do not import it and then run it.

Give each file that you make a name that no other file has, because ZEN keeps a
file in its cache by name at a new import. Test the file one time on the
microscope before you use it for a batch run.

The commands of this module are::

    python MS_czexp_editor.py <file.czexp>              # Print a summary.
    python MS_czexp_editor.py <file.czexp> fit <fov_um> # Try a crop fit.
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

# The hardware limits of the LSM frame size. They come from FrameSizeMin and
# FrameSizeMax in the base .czexp file.
FRAME_SIZE_MIN = 32
FRAME_SIZE_MAX = 5120

# These FocusStrategy values mean that no strategy does anything.
FOCUS_OFF_VALUES = {"", "None", "NONE", "none"}

# These <FocusZStackMode> values control how ZEN reads First and Last of the
# z-stack. Each other value, and a field that is missing, is not known. Refuse
# such a file. Do not guess.
FOCUS_Z_STACK_MODES = {
    "CurrentZ": "relative",   # Offsets from the focus at the position.
    "FixedZ": "absolute",     # An absolute Z. IsCenterMode has no effect.
}


# ---------------------------------------------------------------------------
# Load and save
# ---------------------------------------------------------------------------

def load_czexp(path: PathLike) -> Tuple[ET.ElementTree, ET.Element]:
    """Read a ``.czexp`` file and return ``(tree, root)``."""
    path = Path(path)
    tree = ET.parse(path)
    return tree, tree.getroot()


def save_czexp(tree: ET.ElementTree, path: PathLike) -> Path:
    """Write a ``.czexp`` file as UTF-8, with a BOM and an XML declaration.

    This is the style of ZEN. The function writes to ``<name>.tmp`` first, then
    it replaces the file. Thus a crash never leaves one half of an experiment.
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
# The small helper functions that read and format values
# ---------------------------------------------------------------------------

def _fmt_float(v: float) -> str:
    """Make the shortest text of a float that Python can read back.

    An example of the result is ``0.00111297``.
    """
    return repr(float(v))


def _parse_pair(text: str) -> Tuple[float, float]:
    """Change a text such as ``"1859,1859"`` into ``(1859.0, 1859.0)``."""
    a, b = text.split(",")[:2]
    return float(a), float(b)


def _require(elem: ET.Element, tag: str) -> ET.Element:
    child = elem.find(tag)
    if child is None:
        raise ValueError(f"<{tag}> not found under <{elem.tag}>")
    return child


# ---------------------------------------------------------------------------
# The functions that find an element
# ---------------------------------------------------------------------------

def _iter_own(root: ET.Element, tag: str) -> List[ET.Element]:
    """Get ``root.iter(tag)``, but not the elements in an <Experiment> below it.

    A software-autofocus strategy holds its own experiment, with its own
    FocusSetup and ZStackSetup. That experiment is not the acquisition of this
    experiment.
    """
    embedded = set()
    for exp in root.iter("Experiment"):
        if exp is not root:
            embedded.update(exp.iter(tag))
    return [e for e in root.iter(tag) if e not in embedded]


def find_lsm_detector(root: ET.Element) -> ET.Element:
    """Get the <Detector Id='MTBLSMImagingDevice'> element of this experiment.

    Each function in this module that finds an element ignores a
    software-autofocus experiment below the root. See ``_iter_own``.
    """
    for det in _iter_own(root, "Detector"):
        if det.get("Id") == LSM_DETECTOR_ID:
            return det
    raise ValueError(f"LSM detector '{LSM_DETECTOR_ID}' not found")


def find_tile_regions(root: ET.Element) -> List[ET.Element]:
    """Get all the <SingleTileRegion> elements, which are single-position acquisitions."""
    return _iter_own(root, "SingleTileRegion")


def find_stitch_regions(root: ET.Element) -> List[ET.Element]:
    """Get all the <TileRegion> elements, which are stitch regions of many tiles.

    A <TileRegion> holds its position in <CenterPosition>, in µm. A
    <SingleTileRegion> holds its position in <X> and <Y>.
    """
    return _iter_own(root, "TileRegion")


def find_zstack_setup(root: ET.Element) -> ET.Element:
    """Get the <ZStackSetup> of this experiment that holds First and Last."""
    for zs in _iter_own(root, "ZStackSetup"):
        if zs.find("First") is not None and zs.find("Last") is not None:
            return zs
    raise ValueError("ZStackSetup with First/Last not found")


def is_stitching_configured(root: ET.Element) -> bool:
    """Return ``False`` only if an ACTIVE remote-processing step is empty.

    An active ``<ProcessingStep Id="NULL"/>`` with no child element runs in the
    ZEN user interface. But a run through the API stops with "experiment not
    valid / failed to start". The function returns ``True`` if there is no
    active <ExperimentRemoteProcessingSetup>, or if the setup has a step with
    content. A step has content if its Id is not "NULL", or if it has an
    Algorithm child or a FunctionParameters child.
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
    """Get the active focus strategy, or ``None`` if there is none.

    The result is a dict with the keys "method", "search_action" and
    "surface_mode". The values come from <FocusSetup><FocusStrategy>. The
    strategy is OFF when <Method>, <SearchAction> and <SurfaceMode> are all
    "None", also when <FocusStrategy> keeps IsActivated="true". ZEN can add a
    strategy again when you save an experiment, and it gives no message. Thus
    do this check before you run a file that sets Z itself.
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


# ---------------------------------------------------------------------------
# The safety checks. They raise an exception before you run a .czexp file.
# ---------------------------------------------------------------------------

def require_no_focus_search(root: ET.Element, path: Optional[PathLike] = None,
                            reason: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Raise an exception if the focus strategy of the experiment runs a SEARCH.

    Use this for a run that sets Z itself. A strategy with NO search action is
    permitted. For example, FollowSurface with SearchAction=None uses the Z
    that each region holds. To refuse each strategy, test
    ``get_focus_strategy(root) is None``.

    Args:
        reason: Text that the function adds to the error message.

    Returns:
        The strategy dict, or ``None``.

    Raises:
        ValueError: An active strategy gives a SearchAction, for example
            DefiniteFocus or SoftwareAutofocus.
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
    """Return ``True`` if the <TilesSetup> of this experiment is on.

    ZEN calls this setting Tiles.
    """
    tiles = next(iter(_iter_own(root, "TilesSetup")), None)
    return tiles is not None and (tiles.get("IsActivated") or "false").lower() == "true"


def require_runnable_tiles(root: ET.Element, path: Optional[PathLike] = None) -> None:
    """Raise an exception if Tiles is on but the experiment has no tile region.

    ZEN refuses to start such an experiment, with "Failed to start
    experiment". If Tiles is OFF, ZEN acquires at the current stage position.
    Each <TileRegion> and each <SingleTileRegion> counts. The function does not
    look at IsUsedForAcquisition.

    Raises:
        ValueError: Tiles is on and there is no region.
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
# The readers
# ---------------------------------------------------------------------------

def get_lsm_pixel_size_um(root: ET.Element) -> float:
    """Get the current LSM pixel size in µm.

    The value is ScaledImageRectangleSize divided by FrameSize.
    """
    det = find_lsm_detector(root)
    rect_x, _ = _parse_pair(_require(det, "ScaledImageRectangleSize").text)
    frame_x, _ = _parse_pair(_require(det, "FrameSize").text)
    return rect_x / frame_x


def get_zstack_interval_m(root: ET.Element) -> float:
    """Get the current z-stack interval, which is the step, in metres."""
    zs = find_zstack_setup(root)
    return float(_require(_require(zs, "Interval"), "Distance").find("Value").text)


def get_focus_z_stack_mode(root: ET.Element) -> Optional[str]:
    """Get the <FocusZStackMode> of the experiment.

    The value is "CurrentZ", "FixedZ" or ``None``. THIS field controls how ZEN
    reads First and Last of the z-stack, not <IsCenterMode> (DEV_NOTES
    "FocusZStackMode, not IsCenterMode"). CurrentZ gives offsets from the focus
    at the position. FixedZ gives an absolute Z, also when IsCenterMode is
    true. The function gives the value as the file holds it. Compare it with
    FOCUS_Z_STACK_MODES, or use ``require_focus_z_stack_mode``, before you use
    it. The value is ``None`` if the field is not in the file.

    Raises:
        ValueError: The acquisition blocks of this experiment do not agree.
    """
    modes = {e.text.strip() for e in _iter_own(root, "FocusZStackMode")
             if e.text and e.text.strip()}
    if len(modes) > 1:
        raise ValueError(f"conflicting FocusZStackMode values {sorted(modes)}")
    return modes.pop() if modes else None


def require_focus_z_stack_mode(root: ET.Element, expected: str,
                               path: Optional[PathLike] = None) -> str:
    """Raise an exception if the FocusZStackMode is not ``expected``.

    Use "CurrentZ" if First and Last are relative, and "FixedZ" if they are
    absolute. Call this function before you write a z-stack range. Then an
    offset never goes into a FixedZ file, and an absolute Z never goes into a
    CurrentZ file.

    Returns:
        The mode.

    Raises:
        ValueError: ``expected`` is not a known mode. Or the mode of the file
            is different, missing or not known.
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
    """Return ``True`` if the z-stack is in CENTER mode.

    The field is <IsCenterMode>true</IsCenterMode>. It controls only the user
    interface. To find out if the range is relative or absolute, use
    ``get_focus_z_stack_mode``.
    """
    flag = find_zstack_setup(root).find("IsCenterMode")
    return flag is not None and (flag.text or "").strip().lower() == "true"


def _used_stitch_regions(root: ET.Element) -> List[Tuple[str, ET.Element]]:
    """Get the stitch regions that are in use, in the order of the file.

    The result is a list of ``(name, <TileRegion>)``. A region that has no
    IsUsedForAcquisition flag counts as in use.

    Raises:
        ValueError: A region in use has no Name, or two regions have the same
            Name.
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
    """Get the centre of each stitch region in use, in the order of the file.

    The result is ``{region name: (x_m, y_m)}``. <CenterPosition> is in µm in
    the file, and the function gives metres. A region that has no
    IsUsedForAcquisition flag counts as in use.

    Raises:
        ValueError: A region in use has no Name or no CenterPosition, or two
            regions in use have the same Name.
    """
    centers = {}
    for name, tr in _used_stitch_regions(root):
        x_um, y_um = _parse_pair(_require(tr, "CenterPosition").text)
        centers[name] = (x_um * 1e-6, y_um * 1e-6)
    return centers


def get_tile_region_shape_ids(root: ET.Element) -> Dict[str, Optional[str]]:
    """Get the TemplateShapeId of each stitch region in use.

    The result is ``{region name: TemplateShapeId}``. <TemplateShapeId>
    connects a region to its container on the carrier, which is a well. The
    format is "row-column", and the numbers start at 1. For example, B2 gives
    "2-2". Read this value from an experiment in which ZEN made one region for
    each well. Do not calculate it from the name of the well (DEV_NOTES
    "Re-aiming a tile region at another well"). A region that has no
    TemplateShapeId gives ``None``.

    Raises:
        ValueError: A region in use has no Name, or two regions have the same
            Name.
    """
    ids: Dict[str, Optional[str]] = {}
    for name, tr in _used_stitch_regions(root):
        text = tr.findtext("TemplateShapeId")
        ids[name] = text.strip() if text and text.strip() else None
    return ids


def summarize(root: ET.Element) -> Dict[str, Any]:
    """Get a dict of the current values that this module can change, for the log."""
    det = find_lsm_detector(root)
    zs = find_zstack_setup(root)
    regions = find_tile_regions(root)

    def zval(tag: str) -> float:
        return float(_require(_require(zs, tag), "Distance").find("Value").text)

    return {
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


# ---------------------------------------------------------------------------
# The writers
# ---------------------------------------------------------------------------

def _require_position(x_m: float, y_m: float, z_m: Optional[float]) -> None:
    """Raise ``ValueError`` if a position in metres is outside ``MS_limits``.

    The function checks X and Y, and also Z if you give it. ZEN moves the stage
    to the values in the file. Thus this check is the same check as for a
    direct move. Call this function before the first write.
    """
    MS_limits.require_xy_position(x_m, y_m)
    if z_m is not None:
        MS_limits.require_z_position(z_m)


def set_position(root: ET.Element, x_m: float, y_m: float, z_m: Optional[float] = None,
                 region_index: int = 0) -> Tuple[float, float, Optional[float]]:
    """Set the position of a SingleTileRegion.

    You give metres, and the function writes µm, which is the value x 1e6.

    <FocusZStackMode> controls the Z, not CENTER mode. With CurrentZ, First and
    Last are offsets from <Z>. Thus <Z> must be the centre Z of the target, not
    the value in the base file. With FixedZ, <Z> does not move the stack. Write
    the absolute range with ``set_zstack_range``.

    Raises:
        ValueError: There is no region, or a position is not finite, or a
            position is outside the limits in ``MS_limits``. The function then
            writes nothing.
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
    """Make sure that a <SingleTileRegion> with a position exists, and set X, Y and Z.

    You give metres, and the function writes µm. If the <SingleTileRegions>
    container is empty, the function makes the region. If not, it changes the
    region that is there. A region uses carrier coordinates. Thus ZEN applies
    the calibration of the carrier. A raw StageService move uses machine
    coordinates.

    With CurrentZ, give the centre Z of the target, because First and Last are
    offsets from <Z>. With FixedZ, <Z> does not move the stack. See
    ``set_position``. If ``z_m`` is ``None``, the function writes 0.

    Raises:
        ValueError: There is no container, or a position is not finite, or a
            position is outside the limits in ``MS_limits``. The function then
            writes nothing.
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
    """Set the CenterPosition of a stitch TileRegion.

    You give metres, and the function writes µm. The function also sets the
    <Z> of the region if you give ``z_m``. Leave ``z_m`` as ``None`` if the
    experiment runs its own autofocus.

    Raises:
        ValueError: There is no stitch region, or no <CenterPosition>. Or you
            gave ``z_m`` and the region has no <Z>. Or a position is not
            finite, or it is outside the limits in ``MS_limits``. The function
            then writes nothing.
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
    """Move a tile region that someone made for one well to a different well.

    The function writes <CenterPosition> again, and also <Z> if you give
    ``z_m``. You give metres, and the function writes µm. It also writes the
    Name, which is the name of the scene in the CZI file, and <TemplateShapeId>
    if the region has one. Take ``shape_id`` from
    ``get_tile_region_shape_ids``. The function does not change the tiling.

    The function refuses a connection to the old container, because the
    container adaptation of ZEN can move the region back (DEV_NOTES "Re-aiming
    a tile region at another well").

    Returns:
        ``{"authored_name", "authored_shape_id"}``, which are the values that
        the function replaced.

    Raises:
        ValueError: The region has a TemplateShapeId and ``shape_id`` is
            ``None``. Or ``shape_id`` is not two numbers in the form
            "row-column".
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
    """Remove the single-tile regions, and keep only the <TileRegion> stitches.

    ZEN acquires each <SingleTileRegion> and each <SingleTileRegionArray> that
    stays in the file as an EXTRA scene. Thus a stitch that you moved to a new
    well also images the old positions.

    Returns:
        The number of regions that the function removed.
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
    """Set First and Last of the z-stack in metres, and the Interval if you give it.

    The file holds METRES in these fields. The function does no change to µm.
    <FocusZStackMode>, NOT <IsCenterMode>, controls how ZEN reads the fields.
    "CurrentZ" makes them offsets from the focus at the position, from -h to
    +h. "FixedZ" makes them an absolute Z, from z0 - h to z0 + h, also in
    CENTER mode.

    Give ``expect_mode`` for the kind of range that you write. The function
    refuses a file in a different mode, or in no mode, before it writes
    anything. With ``expect_mode=None`` the function writes and does NO check.
    Always give ``expect_mode``.

    Raises:
        ValueError: You gave ``expect_mode`` and the file does not agree.
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
    """Change the size of the LSM scan field to ``target_fov_um`` with the Zoom.

    The function puts the field at the centre again. With
    ``frame_size_override=None``, the pixel size does not change, and the
    sampling stays at the Nyquist value of the base file. With
    ``frame_size_override=N``, the frame is N x N, the sampling is NOT the
    Nyquist value, and the function sets SamplingMode to "User".

    The function refuses nothing. Look at these keys in the result.
    ``frame_clamped`` is ``True`` if the function limited the frame to the
    range FRAME_SIZE_MIN to FRAME_SIZE_MAX, and ``frame_size_requested`` holds
    the value before the limit. ``fov_exceeds_max`` is ``True`` if
    ``target_fov_um`` is more than ScaledMaxImageSize. We do not know what ZEN
    does in that condition. Do not use ``actual_fov_um`` then.

    Returns:
        A dict of the values that the function applied.

    Raises:
        ValueError: ``target_fov_um`` is not more than 0.
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

    # The Nyquist frame for this field of view, at the sampling of the base
    # file. The pixel size does not change.
    nyquist_frame = target_fov_um / base_pixel_um

    if frame_size_override is not None:
        requested_frame = int(frame_size_override)
    else:
        requested_frame = int(round(nyquist_frame))
    new_frame = max(FRAME_SIZE_MIN, min(FRAME_SIZE_MAX, requested_frame))

    # At the load, ZEN calculates the frame again from the field of view and
    # the sampling. Set the sampling to a value that agrees, thus the frame
    # stays. The frame is proportional to the sampling at a given field of
    # view.
    new_sampling = base_sampling * new_frame / nyquist_frame

    # Calibrate the zoom from the base file: keep its (zoom x field of view).
    zoom_const = base_zoom_x * base_rect_x
    new_zoom = zoom_const / target_fov_um
    pixel_um = target_fov_um / new_frame

    _require(det, "FrameSize").text = f"{new_frame},{new_frame}"
    _require(det, "Zoom").text = f"{_fmt_float(new_zoom)},{_fmt_float(new_zoom)}"
    _require(det, "Sampling").text = _fmt_float(new_sampling)

    # With "SR", ZEN ignores our FrameSize. With "User", ZEN uses it. Change
    # the mode only if the caller gives a frame size.
    sampling_mode = None
    if frame_size_override is not None:
        sm = det.find("SamplingMode")
        if sm is not None:
            sm.text = "User"
            sampling_mode = "User"

    # The field-of-view fields in µm that ZEN calculates. Put them at the
    # centre of the maximum field, which does not depend on the zoom.
    rect_off = (scaled_max_x - target_fov_um) / 2.0
    _require(det, "ScaledImageRectangleSize").text = (
        f"{_fmt_float(target_fov_um)},{_fmt_float(target_fov_um)}")
    rect = det.find("ScaledImageRectangle")
    if rect is not None:
        rect.text = (f"{_fmt_float(rect_off)},{_fmt_float(rect_off)},"
                     f"{_fmt_float(target_fov_um)},{_fmt_float(target_fov_um)}")

    # Frame and ImageFrame are the scan area in pixels, at the centre of the
    # full field, which is ScaledMaxImageSize divided by pixel_um. These
    # values are the same as the values of ZEN.
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
    """Remove the Optimize and ValidateAndAdaptBeforePerform flags from <RunMode>.

    These flags make ZEN calculate the settings again at the run, for example
    the frame size.

    Returns:
        The new RunMode text, or ``None`` if the file has no <RunMode>.
    """
    rm = root.find("RunMode")
    if rm is None or not rm.text:
        return None
    drop = {"OptimizeBeforePerformEnabled", "ValidateAndAdaptBeforePerformEnabled"}
    keep = [f.strip() for f in rm.text.split(",") if f.strip() and f.strip() not in drop]
    rm.text = ",".join(keep)
    return rm.text


def set_lsm_scan_speed_max(root: ET.Element) -> bool:
    """Set IsMaxScanSpeedSet to true. ZEN then uses the maximum of the hardware.

    Returns:
        ``True``, or ``False`` if the file has no IsMaxScanSpeedSet.
    """
    det = find_lsm_detector(root)
    el = det.find("IsMaxScanSpeedSet")
    if el is not None:
        el.text = "true"
        return True
    return False


def set_lsm_sampling_mode_user(root: ET.Element) -> Optional[str]:
    """Set SamplingMode to User, thus ZEN keeps the FrameSize in the file.

    In the SR mode, ZEN ignores the FrameSize.

    Returns:
        The frame size text that the function locked, or ``None`` if the file
        has no SamplingMode.
    """
    det = find_lsm_detector(root)
    sm = det.find("SamplingMode")
    if sm is None:
        return None
    sm.text = "User"
    fr = det.find("FrameSize")
    return fr.text if fr is not None else "User"


# ---------------------------------------------------------------------------
# The builder: an LSM z-stack that fits one object
# ---------------------------------------------------------------------------

def fit_lsm_stack_to_box(base_path: PathLike, out_path: PathLike, center_m: XYZ,
                         size_m: XYZ, *, expect_mode: str,
                         xy_margin_m: float, z_margin_m: float,
                         z_step_m: Optional[float] = None,
                         frame_size: Optional[int] = None,
                         sampling_mode_user: bool = True, max_scan_speed: bool = False,
                         lock_run_mode: bool = False,
                         forbid_focus_search: Union[bool, str] = False) -> Dict[str, Any]:
    """Write a copy of an LSM z-stack experiment that images the box of one object.

    ``center_m`` is ``(x, y, z)`` and ``size_m`` is ``(sx, sy, sz)``. Together
    they give the box of the object, in metres.

    The copy gets a z-stack over z plus or minus (sz/2 + ``z_margin_m``). The
    function writes the range in the FocusZStackMode of the base file. The copy
    also gets a square field of view of max(sx, sy) + 2 x ``xy_margin_m``, from
    ``fit_lsm_crop``. If the base file has a position region, the function sets
    its X, Y and Z to the centre, in µm. If the base file has no position
    region, the experiment acquires at the current stage position, and the
    caller must move there first.

    The caller is responsible for the safety of the Z that it asks for, with
    its own Z guard or Z limit. This function only writes the range that you
    ask for.

    Args:
        expect_mode: "CurrentZ" or "FixedZ", the mode that the Z handling of
            the caller needs. The function refuses a base file in a different
            mode, or in no mode (DEV_NOTES "FocusZStackMode, not
            IsCenterMode").
        sampling_mode_user: Set SamplingMode to User, thus ZEN keeps the frame
            that the function fitted. With ``False``, only a ``frame_size``
            that you give changes the mode to User.
        max_scan_speed: Call ``set_lsm_scan_speed_max``.
        lock_run_mode: Call ``set_run_mode_lock``.
        forbid_focus_search: ``True``, or a text that gives the reason,
            refuses a base file whose focus strategy runs a search. The
            function always refuses a base file with Tiles on and no region.

    Returns:
        A dict with these keys: out_path, focus_z_stack_mode,
        positioned_by_region, z_reference, first_m, last_m, half_z_m,
        z_step_m, n_slices, max_scan_speed and run_mode. It also holds the
        values from ``fit_lsm_crop``. ``z_reference`` is "absolute",
        "region_z" or "current_z". A value of "current_z" means that the
        caller must move Z to the centre z before the run. ``first_m`` and
        ``last_m`` are the values that the function wrote.

    Raises:
        ValueError: The mode does not agree. Or the function refuses the base
            file for a reason above. Or the size gives no field of view.
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
    if expect_mode == "CurrentZ":                  # Offsets from the focus.
        first, last = -half_z, half_z
        z_reference = "region_z" if regions else "current_z"
    else:                                          # FixedZ gives an absolute Z.
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
# The commands: examine a .czexp file, or try a fit
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
