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
Read the acquisition metadata from a ZEN ``.czexp`` experiment.

This module reads the positions and the sample carrier. It also compares a
carrier with the carrier that you expect. It is read-only and uses no ZEN API.
Lengths are in µm in the files, and in metres in each return value. The
exception is a name that ends in ``_um``. To read a ``.czi`` file, use
``ia_common.czi.meta``, for example its ``sample_carrier`` function. SmartMic
does not depend on ``ia_common``.
"""

import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from MS_zenapi_helpers import set_logging


class Position(TypedDict):
    """One stage position in use, from a position file (``load_positions_from_czexp``)."""
    well: str            # For example "D3".
    position_name: str   # For example "P1".
    scene_index: int     # The index over ALL the regions, in the order of the file.
    x_m: float
    y_m: float
    z_m: float


def load_positions_from_czexp(file_path: Path) -> List[Position]:
    """
    Load the positions in use from a ZEN ``.czexp`` file, in metres.

    The file holds µm, and the function changes the values to metres. A well or
    a position that has no ``IsUsedForAcquisition`` flag counts as in use.
    ``scene_index`` counts ALL the regions in the order of the file, in use or
    not. This is the scene number of ZEN. The function does not count the
    regions again after it removes the regions that are not in use.

    Returns:
        One ``Position`` dict for each position in use.

    Raises:
        FileNotFoundError: *file_path* does not exist.
        ValueError: A position in use has no ``<X>`` or no ``<Y>``, or its
            ``<X>``, ``<Y>`` or ``<Z>`` is not a finite number. A ``<Z>`` that
            is missing or empty gives ``z_m = 0.0``. The function does not
            check a position that is not in use.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"czexp file not found: {file_path}")

    def _bool(text: str) -> bool:
        # A flag that is missing or empty means IN USE, as in ZEN. ZEN writes
        # the flag only when its value is False.
        return (text or "true").strip().lower() == "true"

    def _um(region: ET.Element, tag: str, well: str, required: bool) -> float:
        # A coordinate that the function cannot read must stop the run,
        # because a value of 0.0 sends the stage to (0, 0).
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
        well_id = array.get("Name", "")
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
# The sample carrier (the ZEN SampleHolder "Template") from a .czi or .czexp
# --------------------------------------------------------------------------
# The carrier of the experiment is SampleHolder/Template. A SWAF block or a
# stitching block holds a copy under ScanAreaSampleHolderInfo. The name of the
# copy has a ".czsht" suffix.
_CARRIER_TEMPLATE_PARENTS = ("SampleHolder", "ScanAreaSampleHolderInfo")


def strip_czsht(name: str) -> str:
    """Remove the ZEN ``.czsht`` suffix from a carrier template name.

    The comparison ignores the letter case.
    """
    return name[:-len(".czsht")] if name.lower().endswith(".czsht") else name


def _parse_sample_carrier(root: ET.Element, source: str = "") -> Optional[Dict[str, Any]]:
    """
    Read the sample-carrier Template from ``.czexp`` XML that Python parsed.

    The function uses the first ``SampleHolder/Template``. If there is none, it
    uses a ``ScanAreaSampleHolderInfo/Template``. It writes a warning if the
    file gives more than one carrier. The file holds µm, and the function
    changes the values to metres. It returns ``None`` if there is no Template.
    For the keys, see ``get_sample_carrier_from_czexp``.
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
        return None if v != v else v          # A NaN becomes None.

    def _int(tag: str) -> Optional[int]:
        v = _float(tag)
        return int(v) if v is not None else None

    def _m(tag: str) -> Optional[float]:      # Change µm to metres.
        v = _float(tag)
        return v / 1e6 if v is not None else None

    def _pair_m(tag: str) -> Optional[Tuple[float, float]]:   # An "x,y" pair, µm to m.
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


def get_sample_carrier_from_czexp(czexp_path: Path) -> Optional[Dict[str, Any]]:
    """
    Read the sample carrier that a ``.czexp`` file is set up for.

    The data comes from ``SampleHolder/Template``.

    Returns:
        A dict. Each value can be ``None``. The keys are ``name``,
        ``category``, ``shape_type``, ``rows``, ``columns``, ``well_width_m``,
        ``well_height_m``, ``pitch_x_m`` and ``pitch_y_m`` (centre to centre),
        ``bounds_size_m`` and ``anchor_point_m`` as ``(x, y)``,
        ``bottom_material``, ``bottom_thickness_m``, ``insert_type``
        (``"Default"`` or ``"PartOfInsert"``), ``part_of_insert_name`` and
        ``is_part_of_insert``. The lengths are in metres, and the file holds
        µm. The function returns ``None`` if it cannot read the file, or if the
        file has no carrier. ``ia_common.czi.meta.sample_carrier`` returns the
        same dict from a CZI file.

    CAUTION, inserts: for an insert that holds slides, the file records only
    the ONE SLIDE, and ``is_part_of_insert`` is ``True``. The file records
    neither the name of the insert nor the slot. Thus ``name`` is equal to the
    ``get_sample_carrier_name()`` of the API only for a carrier that is not an
    insert. Use ``check_sample_carrier``. See DEV_NOTES "What a CZI / .czexp
    records about the sample carrier".
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
    Compare a carrier dict with the carrier that you expect.

    The dict comes from ``get_sample_carrier_from_czexp``, or from
    ``ia_common.czi.meta.sample_carrier`` for a CZI file.

    Args:
        expected_carrier: The ZEN carrier name as the API reports it. The
                          function compares it only for a carrier that is not
                          an insert.
        expected_slide:   For an insert carrier, the slide Template that the
                          file must record.

    Returns:
        ``(ok, message)``. ``ok`` is ``True`` or ``False``. It is ``None`` if
        the function cannot do the comparison. This occurs if there is no
        carrier, if you give no expected value, or if the carrier is an insert
        and you give no ``expected_slide``, because the file does not record
        the insert.
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
