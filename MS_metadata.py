# -*- coding: utf-8 -*-

#################################################################
# File        : MS_metadata.py
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
Read acquisition metadata from ZEN files: ``.czexp`` experiments and ``.czi`` images.

Read-only, no ZEN API. Lengths are µm in the files and metres in every return
value, except where a name says ``_um``. The CZI readers import ``pylibCZIrw``
when called.
"""

import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from MS_zenapi_helpers import set_logging


class Position(TypedDict):
    """One used stage position of a position file (``load_positions_from_czexp``)."""
    well: str            # e.g. "D3"
    position_name: str   # e.g. "P1"
    scene_index: int     # index in file order over ALL regions, used or not
    x_m: float
    y_m: float
    z_m: float


def load_positions_from_czexp(file_path: Path) -> List[Position]:
    """
    Load the used positions of a ZEN .czexp, converted from µm to metres.

    A well or position without an ``IsUsedForAcquisition`` flag counts as used.
    ``scene_index`` counts ALL regions in file order, used or not (ZEN's scene
    numbering); it is not re-numbered after filtering.

    Returns:
        One ``Position`` dict per used position.

    Raises:
        FileNotFoundError: if *file_path* does not exist.
        ValueError: if a used position has no ``<X>`` or ``<Y>``, or an
            ``<X>``/``<Y>``/``<Z>`` that is not a finite number. A missing or
            empty ``<Z>`` gives ``z_m = 0.0``; unused positions are not checked.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"czexp file not found: {file_path}")

    def _bool(text: str) -> bool:
        # Missing/empty → USED, as in ZEN (the flag is only written when False).
        return (text or "true").strip().lower() == "true"

    def _um(region: ET.Element, tag: str, well: str, required: bool) -> float:
        # An unreadable coordinate must stop the run: a silent 0.0 would send
        # the stage to (0, 0).
        text = (region.findtext(tag) or "").strip()
        where = f"{file_path.name}: well '{well}', position '{region.get('Name', '')}'"
        if not text:
            if required:
                raise ValueError(f"{where}: <{tag}> is missing or empty")
            return 0.0
        try:
            value = float(text)
        except ValueError:
            value = math.nan
        if not math.isfinite(value):
            raise ValueError(f"{where}: <{tag}> is not a finite number ({text!r})")
        return value

    root = ET.parse(file_path).getroot()
    positions = []
    scene_idx = 0

    for array in root.iter("SingleTileRegionArray"):
        well_used = _bool(array.findtext("IsUsedForAcquisition"))
        well_id   = array.get("Name", "")
        regions_el = array.find("SingleTileRegions")
        if regions_el is None:
            continue
        for region in regions_el.findall("SingleTileRegion"):
            pos_used = _bool(region.findtext("IsUsedForAcquisition"))
            if well_used and pos_used:
                positions.append({
                    "well":          well_id,
                    "position_name": region.get("Name", ""),
                    "scene_index":   scene_idx,
                    "x_m":           _um(region, "X", well_id, required=True) / 1e6,
                    "y_m":           _um(region, "Y", well_id, required=True) / 1e6,
                    "z_m":           _um(region, "Z", well_id, required=False) / 1e6,
                })
            scene_idx += 1

    set_logging().info(f"Loaded {len(positions)} positions from {file_path.name}")
    return positions


# --------------------------------------------------------------------------
# Sample carrier (ZEN SampleHolder "Template") from a .czi / .czexp
# --------------------------------------------------------------------------
# The experiment's own carrier is SampleHolder/Template; SWAF/stitching blocks
# carry a copy under ScanAreaSampleHolderInfo, named with a ".czsht" suffix.
_CARRIER_TEMPLATE_PARENTS = ("SampleHolder", "ScanAreaSampleHolderInfo")


def strip_czsht(name: str) -> str:
    """A carrier template name without ZEN's ``.czsht`` file suffix (case is ignored)."""
    return name[:-len(".czsht")] if name.lower().endswith(".czsht") else name


def _parse_sample_carrier(root: ET.Element, source: str = "") -> Optional[Dict[str, Any]]:
    """
    Read the sample-carrier Template from parsed .czi metadata or .czexp XML.

    Prefers the first ``SampleHolder/Template``, else a
    ``ScanAreaSampleHolderInfo/Template``; warns if the file names more than
    one carrier. µm in the file → metres. ``None`` if there is no Template.
    Keys: see ``get_sample_carrier_from_czi``.
    """
    found = [(parent.tag, child)
             for parent in root.iter()
             for child in parent
             if child.tag == "Template" and parent.tag in _CARRIER_TEMPLATE_PARENTS]
    if not found:
        return None
    tmpl = next((t for p, t in found if p == "SampleHolder"), found[0][1])

    names = {strip_czsht(t.get("Name", "")) for _, t in found}
    if len(names) > 1:
        set_logging().warning(
            f"{source or 'metadata'}: more than one sample carrier "
            f"({', '.join(sorted(names))}); using '{strip_czsht(tmpl.get('Name', ''))}'.")

    def _float(tag: str) -> Optional[float]:
        try:
            v = float(tmpl.findtext(tag))
        except (TypeError, ValueError):
            return None
        return None if v != v else v          # NaN → None

    def _int(tag: str) -> Optional[int]:
        v = _float(tag)
        return int(v) if v is not None else None

    def _m(tag: str) -> Optional[float]:      # µm → m
        v = _float(tag)
        return v / 1e6 if v is not None else None

    def _pair_m(tag: str) -> Optional[Tuple[float, float]]:   # "x,y" µm → m
        try:
            x, y = (float(v) / 1e6 for v in tmpl.findtext(tag).split(","))
        except (AttributeError, TypeError, ValueError):
            return None
        return (x, y)

    def _text(tag: str) -> Optional[str]:
        t = tmpl.findtext(tag)
        return t.strip() if t and t.strip() else None

    insert_type = _text("InsertType")
    return {
        "name":                strip_czsht(tmpl.get("Name", "")),
        "category":            _text("Category"),
        "shape_type":          _text("ShapeType"),
        "rows":                _int("ShapeRows"),
        "columns":             _int("ShapeColumns"),
        "well_width_m":        _m("ShapeWidth"),
        "well_height_m":       _m("ShapeHeight"),
        "pitch_x_m":           _m("ShapeDistanceX"),
        "pitch_y_m":           _m("ShapeDistanceY"),
        "bounds_size_m":       _pair_m("BoundsSize"),
        "anchor_point_m":      _pair_m("AnchorPoint"),
        "bottom_material":     _text("BottomMaterial"),
        "bottom_thickness_m":  _m("BottomThickness"),
        "insert_type":         insert_type,
        "part_of_insert_name": _text("PartOfInsertName"),
        "is_part_of_insert":   insert_type == "PartOfInsert",
    }


def get_sample_carrier_from_czi(czi_path: Path) -> Optional[Dict[str, Any]]:
    """
    Read the sample carrier a CZI was acquired with (``SampleHolder/Template``).

    Returns a dict (any value may be ``None``): ``name``, ``category``,
    ``shape_type``, ``rows``, ``columns``, ``well_width_m``,
    ``well_height_m``, ``pitch_x_m``/``pitch_y_m`` (centre to centre),
    ``bounds_size_m`` and ``anchor_point_m`` as ``(x, y)``,
    ``bottom_material``, ``bottom_thickness_m``, ``insert_type``
    (``"Default"``/``"PartOfInsert"``), ``part_of_insert_name``,
    ``is_part_of_insert``. Lengths in metres (µm in the file). ``None`` if
    the file is unreadable or holds no carrier.

    CAUTION, inserts: for an insert holding slides the file records only the
    ONE SLIDE (``is_part_of_insert`` True), not the insert's name nor the slot.
    So ``name`` equals the API's ``get_sample_carrier_name()`` only for a
    carrier that is not an insert. Use ``check_sample_carrier``. DEV_NOTES
    "What a CZI / .czexp records about the sample carrier".
    """
    try:
        from pylibCZIrw import czi as pyczi
        with pyczi.open_czi(str(czi_path)) as czidoc:
            root = ET.fromstring(czidoc.raw_metadata)
    except Exception as e:
        set_logging().warning(f"Sample carrier: could not read the file ({type(e).__name__}: {e})")
        return None
    return _parse_sample_carrier(root, Path(czi_path).name)


def get_sample_carrier_from_czexp(czexp_path: Path) -> Optional[Dict[str, Any]]:
    """
    Read the sample carrier a .czexp is set up for.

    Same dict, and the same caution about inserts, as
    ``get_sample_carrier_from_czi``. ``None`` if unreadable or no carrier.
    """
    try:
        root = ET.parse(str(czexp_path)).getroot()
    except Exception as e:
        set_logging().warning(f"Sample carrier: could not read the file ({type(e).__name__}: {e})")
        return None
    return _parse_sample_carrier(root, Path(czexp_path).name)


def check_sample_carrier(carrier: Optional[Dict[str, Any]],
                         expected_carrier: Optional[str] = None,
                         expected_slide: Optional[str] = None,
                         ) -> Tuple[Optional[bool], str]:
    """
    Check a carrier from ``get_sample_carrier_from_czi`` / ``_czexp``.

    Args:
        expected_carrier: ZEN carrier name as the API reports it; compared only
                          for a carrier that is not an insert.
        expected_slide:   For an insert carrier, the slide Template the file
                          should record.

    Returns:
        ``(ok, message)``. ``ok`` is True / False, or None if it can't be
        checked (no carrier, nothing expected, or an insert without
        ``expected_slide``: the file doesn't record the insert).
    """
    if carrier is None:
        return None, "no sample carrier in the file"
    name = carrier["name"]

    if carrier["is_part_of_insert"]:
        if expected_slide is None:
            return None, (f"slide '{name}' in an insert: the file records only the "
                          f"slide, not the insert or its slot, so carrier "
                          f"'{expected_carrier}' can't be checked from it")
        if name == expected_slide:
            return True, f"slide '{name}' (in an insert) as expected"
        return False, f"slide '{name}' (in an insert), expected '{expected_slide}'"

    if expected_carrier is None:
        return None, f"carrier '{name}': no expected carrier given"
    if name == expected_carrier:
        return True, f"carrier '{name}' as expected"
    return False, f"carrier '{name}', expected '{expected_carrier}'"


_FOCUS_POS_RE = re.compile(
    rb"<FocusPosition>\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*</FocusPosition>")
# The first <FocusPosition> is near the start of a ZEN-written CZI (~0.8 MB in
# the fixtures). A file with more metadata gives None, not a wrong value.
_CZI_READ_BYTES = 8 * 1024 * 1024


def get_focus_position_from_czi(czi_path: Path) -> Optional[float]:
    """
    The FocusPosition (µm) in a CZI's metadata: the imaging-plane Z.

    Not the raw ZDrive encoder value of the gRPC API. For a z-stack, the first
    plane. Scans only the first 8 MB (no CZI library).

    Returns:
        Focus position in µm, or ``None`` if not found or unreadable.
    """
    try:
        with open(czi_path, "rb") as f:
            chunk = f.read(_CZI_READ_BYTES)
        m = _FOCUS_POS_RE.search(chunk)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


def get_zstack_z_range(czi_path: Path) -> Optional[Dict[str, Any]]:
    """
    Absolute Z (µm) of the first, centre and last planes of a CZI z-stack.

    First plane from ``FocusPosition``; step from ``Scaling/Distance Id="Z"``
    (stored in m, returned in µm).

    Returns:
        Dict with ``first_um``, ``center_um``, ``last_um``, ``n_z``,
        ``step_um``, or ``None`` if unreadable or not a z-stack.
    """
    try:
        from pylibCZIrw import czi as pyczi

        first_um = get_focus_position_from_czi(czi_path)
        if first_um is None:
            return None

        with pyczi.open_czi(str(czi_path)) as czidoc:
            bbox    = czidoc.total_bounding_box
            z_range = bbox.get("Z", (0, 1))
            n_z     = z_range[1] - z_range[0]
            if n_z <= 1:
                return None   # single-plane image, not a z-stack

            step_um: Optional[float] = None
            try:
                root = ET.fromstring(czidoc.raw_metadata)
                for dist in root.iter("Distance"):
                    if dist.get("Id") == "Z":
                        val_el = dist.find("Value")
                        if val_el is not None and val_el.text:
                            step_um = float(val_el.text) * 1e6   # m → µm
                            break
            except Exception:
                pass

        if step_um is None:
            return None

        last_um   = first_um + (n_z - 1) * step_um
        center_um = first_um + (n_z - 1) / 2.0 * step_um
        return {
            "first_um":  first_um,
            "center_um": center_um,
            "last_um":   last_um,
            "n_z":       n_z,
            "step_um":   step_um,
        }

    except Exception as e:
        set_logging().warning(f"Z-stack range: could not read the file ({type(e).__name__}: {e})")
        return None


def get_stage_position_from_czi(czi_path: Path) -> Optional[Dict[str, Any]]:
    """
    Read the stage position (metres) a CZI was acquired at.

    Returns a dict (any field may be ``None``), or ``None`` if unreadable:

    - ``planned_x_m`` / ``planned_y_m``: the scene ``CenterPosition``. For an
      experiment with an AUTHORED, used position this is where it imaged. Use it.
    - ``actual_x_m`` / ``actual_y_m``: the ``MTBStageAxisX``/``Y`` encoder.
      CAUTION: usually the PARKED/home position after the scan, not the scan
      position (``IsPrecise=false``). Do not treat it as "where imaged".
    - ``z_m``: ``FocusPosition`` (for a z-stack, the first plane).

    A region merely injected into a non-positioned experiment is ignored by
    ZEN; CenterPosition then need not match where it imaged (verify by image
    content).
    """
    try:
        from pylibCZIrw import czi as pyczi
        with pyczi.open_czi(str(czi_path)) as czidoc:
            root = ET.fromstring(czidoc.raw_metadata)
    except Exception as e:
        set_logging().warning(f"Stage position: could not read the file ({type(e).__name__}: {e})")
        return None

    actual_x_m = actual_y_m = None
    for pc in root.iter("ParameterCollection"):
        cid = pc.get("Id", "")
        pos_elem = pc.find("Position")
        if pos_elem is None or pos_elem.text is None:
            continue
        try:
            val_m = float(pos_elem.text) / 1e6   # µm → m
        except ValueError:
            continue
        if cid == "MTBStageAxisX":
            actual_x_m = val_m
        elif cid == "MTBStageAxisY":
            actual_y_m = val_m

    # Planned target XY (scene CenterPosition, "X,Y" in µm).
    planned_x_m = planned_y_m = None
    scene = next(root.iter("Scene"), None)
    if scene is not None:
        cp = scene.findtext("CenterPosition")
        if cp:
            try:
                planned_x_m, planned_y_m = (float(v) / 1e6 for v in cp.split(","))
            except ValueError:
                pass

    z_um = get_focus_position_from_czi(czi_path)
    return {
        "actual_x_m":  actual_x_m,
        "actual_y_m":  actual_y_m,
        "planned_x_m": planned_x_m,
        "planned_y_m": planned_y_m,
        "z_m":         (z_um / 1e6) if z_um is not None else None,
    }


def get_xy_position_from_czi(czi_path: Path, prefer: str = "planned"
                             ) -> Optional[Tuple[float, float]]:
    """
    The stage XY (metres) a CZI was acquired at, as a tuple.

    ``prefer="planned"`` (default): ``CenterPosition``, else the encoder.
    ``prefer="actual"``: the encoder first, which is usually the PARKED home
    position (see :func:`get_stage_position_from_czi`). ``None`` if neither.
    """
    info = get_stage_position_from_czi(czi_path)
    if info is None:
        return None
    actual = ((info["actual_x_m"], info["actual_y_m"])
              if info["actual_x_m"] is not None and info["actual_y_m"] is not None
              else None)
    planned = ((info["planned_x_m"], info["planned_y_m"])
               if info["planned_x_m"] is not None and info["planned_y_m"] is not None
               else None)
    primary, fallback = (actual, planned) if prefer == "actual" else (planned, actual)
    return primary if primary is not None else fallback
