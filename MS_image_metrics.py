# -*- coding: utf-8 -*-

#################################################################
# File        : MS_image_metrics.py
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
Image metrics on a ``.czi``: focus score and signal level.

Read-only, no ZEN API. Needs ``pylibCZIrw`` and ``numpy`` (imported when
called). ``MS_Helper_function`` re-exports every name.

Public functions
----------------
compute_focus_score      -- Laplacian variance of the sharpest plane
signal_level_from_czi    -- 99.9th-percentile intensity / detector full scale
is_czi_effectively_empty -- True if that level is below EMPTY_STACK_SIGNAL_FRAC
"""

from pathlib import Path
from typing import Optional

from MS_zenapi_helpers import set_logging


def compute_focus_score(czi_path: Path) -> Optional[float]:
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
        Best Laplacian variance (always ≥ 0), or None if the file
               cannot be read.
    """
    try:
        from pylibCZIrw import czi as pyczi
        import numpy as np

        def _lap_var(arr: np.ndarray) -> float:
            # Interior pixels only: np.roll would wrap, i.e. compare the first
            # row/column with the last one.
            lap = (
                arr[:-2, 1:-1] + arr[2:, 1:-1] +
                arr[1:-1, :-2] + arr[1:-1, 2:] -
                4.0 * arr[1:-1, 1:-1]
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
            set_logging().info(f"Focus score: sharpest plane Z={best_z} (of {n_z}), "
                               f"score={best_score:.1f}")
            return best_score

    except Exception as e:
        set_logging().warning(f"Focus score: could not read the file ({type(e).__name__}: {e})")
        return None


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
        set_logging().warning(f"Signal level: could not read the file ({type(e).__name__}: {e})")
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
