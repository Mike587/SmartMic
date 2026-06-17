# -*- coding: utf-8 -*-

#################################################################
# File        : MS_Helper_function.py
# Author      : Mike Stebler
# Institution : ETH Zurich | ScopeM
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

    # Wrap stdout with UTF-8 so special characters don't crash on Windows cp1252 terminals
    import io
    sh_stream = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace") \
        if hasattr(sys.stdout, "buffer") else sys.stdout
    sh = logging.StreamHandler(sh_stream)
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
                "scene_index":   int,
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
