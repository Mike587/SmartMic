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
Read acquisition metadata from ZEN ``.czexp`` experiments: positions and the
sample carrier, and check a carrier against the expected one.

Read-only, no ZEN API. Lengths are µm in the files and metres in every return
value, except where a name says ``_um``. Reading a ``.czi`` is ia_common's job
(``ia_common.czi.meta``, e.g. ``sample_carrier``); SmartMic does not depend on it.
"""

import math
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
    Read the sample-carrier Template from parsed .czexp XML.

    Prefers the first ``SampleHolder/Template``, else a
    ``ScanAreaSampleHolderInfo/Template``; warns if the file names more than
    one carrier. µm in the file → metres. ``None`` if there is no Template.
    Keys: see ``get_sample_carrier_from_czexp``.
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


def get_sample_carrier_from_czexp(czexp_path: Path) -> Optional[Dict[str, Any]]:
    """
    Read the sample carrier a .czexp is set up for (``SampleHolder/Template``).

    Returns a dict (any value may be ``None``): ``name``, ``category``,
    ``shape_type``, ``rows``, ``columns``, ``well_width_m``,
    ``well_height_m``, ``pitch_x_m``/``pitch_y_m`` (centre to centre),
    ``bounds_size_m`` and ``anchor_point_m`` as ``(x, y)``,
    ``bottom_material``, ``bottom_thickness_m``, ``insert_type``
    (``"Default"``/``"PartOfInsert"``), ``part_of_insert_name``,
    ``is_part_of_insert``. Lengths in metres (µm in the file). ``None`` if
    unreadable or no carrier. ``ia_common.czi.meta.sample_carrier`` returns the
    same dict from a CZI.

    CAUTION, inserts: for an insert holding slides the file records only the
    ONE SLIDE (``is_part_of_insert`` True), not the insert's name nor the slot.
    So ``name`` equals the API's ``get_sample_carrier_name()`` only for a
    carrier that is not an insert. Use ``check_sample_carrier``. DEV_NOTES
    "What a CZI / .czexp records about the sample carrier".
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
    Check a carrier dict (``get_sample_carrier_from_czexp``, or
    ``ia_common.czi.meta.sample_carrier`` for a CZI) against the expected one.

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
