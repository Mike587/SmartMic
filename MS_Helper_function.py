# -*- coding: utf-8 -*-

#################################################################
# File        : MS_Helper_function.py
# Author      : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#
# Copyright(c) 2026 ETH Zurich (ScopeM). All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

import logging
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Global variable for the default experiment output folder
DEFAULT_EXPERIMENT_OUTPUT_FOLDER = Path("F:/UserData/mike/api")

# Default folder for run log files
DEFAULT_LOG_FOLDER = DEFAULT_EXPERIMENT_OUTPUT_FOLDER / "log"


def setup_run_logger(
    log_folder: Path = None,
    name: str = "smartmic",
) -> Tuple[logging.Logger, Path]:
    """
    Create a timestamped per-run log file and return a logger that emits to
    both the log file and stdout.

    Args:
        log_folder: Directory for the log file.
                    Defaults to DEFAULT_LOG_FOLDER (F:/UserData/mike/api/log).
        name:       Logger name passed to logging.getLogger().
                    Use different names when running multiple independent
                    scripts in the same process to avoid handler collisions.

    Returns:
        Tuple ``(logger, log_file)`` — the configured ``logging.Logger`` and
        the ``Path`` to the timestamped per-run log file it writes to.

    Note:
        Uses the same logger name (``"smartmic"``) and line format as
        ``MS_zenapi_helpers.set_logging``.  Call this once at the start of a run
        to attach the per-run file handler; the wrapper modules' ``set_logging``
        then reuses this same configured logger, so their output also lands in
        the run log.  Keep the format below in sync with that helper.
    """
    if log_folder is None:
        log_folder = DEFAULT_LOG_FOLDER
    log_folder = Path(log_folder)
    log_folder.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file  = log_folder / f"run_{timestamp}.log"

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # Clear any handlers left over from a previous call so we never
    # accumulate duplicates when the function is called more than once.
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Make stdout encode UTF-8 so special characters (e.g. µ) don't crash on a
    # Windows cp1252 terminal.  reconfigure() mutates the existing stream in
    # place — unlike wrapping it in a new TextIOWrapper, which would close the
    # shared buffer when dropped and break any other stdout handler.
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info(f"Run log: {log_file}")
    return logger, log_file



def load_positions_from_czexp(file_path: Path) -> List[dict]:
    """
    Load well-plate positions directly from a ZEN .czexp experiment file.

    Parses the experiment XML with the stdlib ``xml.etree`` parser and returns a
    flat list of position dicts. Coordinates are converted from µm (as stored in
    the .czexp) to metres. Only positions with ``is_used == True`` are returned.

    Args:
        file_path: Path to the .czexp file.

    Returns:
        A flat list of dicts, one entry per position::

            {
                "well":          str,   # e.g. "D3"
                "position_name": str,   # e.g. "P1"
                "scene_index":   int,   # index in file order across ALL regions,
                                        # used or not — matches ZEN's own scene
                                        # numbering; NOT re-numbered after filtering.
                "x_m":           float, # metres
                "y_m":           float, # metres
                "z_m":           float, # metres
            }

    Raises:
        FileNotFoundError: if *file_path* does not exist.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"czexp file not found: {file_path}")

    def _bool(text: str) -> bool:
        # Default to True when the tag is missing/empty: a SingleTileRegion
        # without an explicit IsUsedForAcquisition flag is treated as USED,
        # matching ZEN's own behaviour (the flag is only written when False).
        return (text or "true").strip().lower() == "true"

    def _float(text: str, fallback: float = 0.0) -> float:
        try:
            return float(text or fallback)
        except (ValueError, TypeError):
            return fallback

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
                    "x_m":           _float(region.findtext("X")) / 1e6,
                    "y_m":           _float(region.findtext("Y")) / 1e6,
                    "z_m":           _float(region.findtext("Z")) / 1e6,
                })
            scene_idx += 1

    print(f"[INFO] Loaded {len(positions)} positions from {file_path.name}")
    return positions


def compute_focus_score(czi_path: Path) -> float | None:
    """
    Compute a Laplacian-variance sharpness metric for a CZI file.

    For single-plane images the one plane is used directly.  For z-stacks the
    function scores every Z-plane (channel 0, time 0) and returns the maximum
    score together with the index of the sharpest plane.

    Formula: Var( ∇²I )  where ∇²I is the discrete 2-D Laplacian.
    Higher score = sharper / better in focus.

    Uses pylibCZIrw for pixel reading and plain numpy for the metric —
    no extra dependencies beyond what the smartmic env already has.

    Returns:
        float: best Laplacian variance (always ≥ 0), or None if the file
               cannot be read.
    """
    try:
        from pylibCZIrw import czi as pyczi
        import numpy as np

        def _lap_var(arr: np.ndarray) -> float:
            lap = (
                np.roll(arr,  1, axis=0) + np.roll(arr, -1, axis=0) +
                np.roll(arr,  1, axis=1) + np.roll(arr, -1, axis=1) -
                4.0 * arr
            )
            return float(np.var(lap))

        with pyczi.open_czi(str(czi_path)) as czidoc:
            bbox = czidoc.total_bounding_box          # {"Z": (first, last+1), ...}
            z_range = bbox.get("Z", (0, 1))           # (first_index, last_index+1)
            n_z = z_range[1] - z_range[0]

            if n_z <= 1:
                # Single plane — read it directly
                img = czidoc.read(plane={"C": 0, "T": 0, "Z": z_range[0]})
                return _lap_var(img[..., 0].astype(np.float32))

            # Z-stack — score every plane and return the best
            best_score = -1.0
            best_z     = z_range[0]
            for z in range(z_range[0], z_range[1]):
                img   = czidoc.read(plane={"C": 0, "T": 0, "Z": z})
                score = _lap_var(img[..., 0].astype(np.float32))
                if score > best_score:
                    best_score = score
                    best_z     = z
            print(f"[FOCUS_SCORE] sharpest plane Z={best_z} (of {n_z}), score={best_score:.1f}")
            return best_score

    except Exception as e:
        print(f"[FOCUS_SCORE ERROR] {type(e).__name__}: {e}")
        return None


_FOCUS_POS_RE = re.compile(rb"<FocusPosition>([\+\-]?[\d.]+)</FocusPosition>")
_CZI_READ_BYTES = 8 * 1024 * 1024  # CZI metadata always precedes pixel data


def get_focus_position_from_czi(czi_path: Path) -> Optional[float]:
    """
    Extract the FocusPosition (µm) stored by ZEN in a CZI file's XML metadata.

    FocusPosition is the actual imaging-plane Z as recorded by the acquisition
    engine — distinct from the raw ZDrive encoder value returned by the gRPC
    API.  For a z-stack this is the Z of the first acquired plane.

    Only the first 8 MB of the file are read because the ZISRAWMETADATA
    segment always precedes image data in well-formed CZI files.

    Args:
        czi_path: Path to the .czi file.

    Returns:
        Focus position in µm, or ``None`` if not found or the file is
        unreadable.
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


def get_zstack_z_range(czi_path: Path) -> Optional[Dict]:
    """
    Return the absolute Z positions (µm) of the first, center, and last planes
    of a CZI z-stack, together with the number of planes and the step size.

    Uses:
    - ``FocusPosition`` from the CZI binary header → absolute Z of the first plane.
    - ``total_bounding_box`` from pylibCZIrw         → number of Z planes.
    - ``<Scaling><Distance Id="Z"><Value>`` in the embedded XML → step size (m → µm).

    Args:
        czi_path: Path to the .czi file.

    Returns:
        Dict with keys ``first_um``, ``center_um``, ``last_um``, ``n_z``,
        ``step_um``, or ``None`` if the file cannot be read or is not a z-stack.
    """
    try:
        from pylibCZIrw import czi as pyczi

        # First-plane absolute Z — reuse get_focus_position_from_czi
        first_um = get_focus_position_from_czi(czi_path)
        if first_um is None:
            return None

        # --- number of planes and step size from embedded XML ---
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
        print(f"[ZSTACK_RANGE ERROR] {type(e).__name__}: {e}")
        return None


def get_stage_position_from_czi(czi_path: Path) -> Optional[Dict]:
    """
    Read the stage position (metres) a CZI was acquired at, from its embedded
    ZEN metadata.  Returns a dict (any field may be ``None``):

    - ``planned_x_m`` / ``planned_y_m`` — the scene ``CenterPosition``: the position
      the acquisition is set to.  For an experiment with an AUTHORED, used position
      (use-positions mode), this is where the stage navigates to and images — i.e.
      WHERE THE IMAGE WAS TAKEN.  This is the reliable field; use it.
    - ``actual_x_m`` / ``actual_y_m`` — the ``MTBStageAxisX``/``Y`` encoder value in
      the metadata.  CAUTION: this is typically the PARKED/home position, not the
      scan position — a positioned acquisition moves to the target, images, then
      returns the stage home, and the encoder here reflects that parked spot
      (observed: encoder = the start position even when the scan navigated
      elsewhere).  ZEN also marks it ``IsPrecise=false``.  Do not treat it as
      "where imaged".
    - ``z_m`` — the focus Z (``FocusPosition``; for a z-stack, the first plane).

    Note: this only reflects a navigated position when the experiment has an
    AUTHORED position (e.g. NfS_image_nuceli_002).  A region merely injected into a
    non-positioned experiment is ignored by ZEN, and CenterPosition then need not
    match where it imaged — verify by image content.  Logic promoted from the
    image-analysis ``get_scene_center_positions`` (ia_PoC_002 / ia_Marc / ia_NfS).
    Returns ``None`` if the file is unreadable.
    """
    try:
        from pylibCZIrw import czi as pyczi
        with pyczi.open_czi(str(czi_path)) as czidoc:
            root = ET.fromstring(czidoc.raw_metadata)
    except Exception as e:
        print(f"[STAGE_POS ERROR] {type(e).__name__}: {e}")
        return None

    # Actual encoder XY (recorded at acquisition).
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
    Convenience getter: the stage XY (metres) a CZI was acquired at, as a tuple.

    ``prefer="planned"`` (default) returns the scene ``CenterPosition`` — the
    acquisition position (where an authored-position experiment navigates to and
    images) — falling back to the encoder if absent.  ``prefer="actual"`` returns
    the ``MTBStageAxis`` encoder first, but note that is usually the PARKED home
    position, not the scan position (see :func:`get_stage_position_from_czi`).
    Returns ``None`` if neither XY is present.
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


# p99.9-intensity / detector-full-scale below this fraction → the stack captured
# (essentially) nothing. Calibrated on the slide (DEV_NOTES "50x DefiniteFocus
# errors"): a dud / failed-DF stack reads ~0.01, focused tissue ~0.10–0.14.
EMPTY_STACK_SIGNAL_FRAC = 0.04


def signal_level_from_czi(czi_path: Path, channel: int = 0) -> Optional[float]:
    """
    Return how much signal a CZI captured, as the 99.9th-percentile intensity
    divided by the detector full-scale (0 = black, ~1 = saturated).

    The 99.9th percentile (not the max) is used so a single hot pixel can't make
    an otherwise-empty stack look bright — a dud stack often still carries a stray
    bright pixel. Normalising by full-scale (``np.iinfo(dtype).max`` for integer
    images) makes the value exposure- and bit-depth-independent, so the same
    threshold works across experiments.

    Intended for confocal stacks (the empty / failed-DF guard); NOT for raw
    Airyscan, which is dim by design before ZEN reconstruction (see DEV_NOTES).
    Logic promoted from the analysis-side ``ia_NfS`` (measure_thickness /
    find_nuclei_bboxes) so a pipeline can make the call without the analysis repo.

    Args:
        czi_path: Path to the .czi file.
        channel:  Channel index to read (default 0 = DAPI).

    Returns:
        The signal level (float ≥ 0), or ``None`` if the file cannot be read.
    """
    try:
        from pylibCZIrw import czi as pyczi
        import numpy as np

        with pyczi.open_czi(str(czi_path)) as czidoc:
            z_range = czidoc.total_bounding_box.get("Z", (0, 1))
            planes = []
            dtype = None
            for z in range(z_range[0], z_range[1]):
                plane = czidoc.read(plane={"C": channel, "T": 0, "Z": z})[..., 0]
                if dtype is None:
                    dtype = plane.dtype
                planes.append(plane)
            if not planes:
                return None
            zyx = np.stack(planes).astype(np.float32)

        full_scale = (float(np.iinfo(dtype).max)
                      if np.issubdtype(dtype, np.integer)
                      else float(zyx.max()) or 1.0)
        return float(np.percentile(zyx, 99.9)) / full_scale

    except Exception as e:
        print(f"[SIGNAL_LEVEL ERROR] {type(e).__name__}: {e}")
        return None


def is_czi_effectively_empty(czi_path: Path,
                             frac: float = EMPTY_STACK_SIGNAL_FRAC,
                             channel: int = 0) -> Optional[bool]:
    """
    Tell whether a CZI captured essentially nothing (a dark / failed-DF stack).

    Thin wrapper over :func:`signal_level_from_czi`: ``True`` when the signal
    level is below ``frac`` of detector full-scale. Lets a pipeline distinguish
    "this acquisition imaged something" from "DF failed / out of focus → dark
    stack" and skip the site, rather than feed a black stack to segmentation
    (Otsu on noise invents spurious detections).

    Args:
        czi_path: Path to the .czi file.
        frac:     Signal-level threshold (default ``EMPTY_STACK_SIGNAL_FRAC``).
        channel:  Channel index to read (default 0 = DAPI).

    Returns:
        ``True`` / ``False``, or ``None`` if the file cannot be read.
    """
    level = signal_level_from_czi(czi_path, channel=channel)
    if level is None:
        return None
    return level < frac
