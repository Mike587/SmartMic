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

Read-only, no ZEN API. ``pylibCZIrw`` and ``numpy`` are imported when called.
"""

from pathlib import Path
from typing import Optional

from MS_zenapi_helpers import set_logging


def compute_focus_score(czi_path: Path) -> Optional[float]:
    """Sharpness of a CZI: Var(∇²I) of the sharpest Z-plane (C=0, T=0).

    Higher = sharper. The sharpest plane index is logged, not returned.

    Returns:
        The best Laplacian variance (≥ 0), or None if the file cannot be read.
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
            bbox = czidoc.total_bounding_box
            z_range = bbox.get("Z", (0, 1))           # (first_index, last_index+1)
            n_z = z_range[1] - z_range[0]

            if n_z <= 1:
                img = czidoc.read(plane={"C": 0, "T": 0, "Z": z_range[0]})
                return _lap_var(img[..., 0].astype(np.float32))

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


# Signal level below this → the stack captured (essentially) nothing.
# Calibration: DEV_NOTES "50x DefiniteFocus errors".
EMPTY_STACK_SIGNAL_FRAC = 0.04


def signal_level_from_czi(czi_path: Path, channel: int = 0) -> Optional[float]:
    """Signal of a CZI: 99.9th-percentile intensity / detector full scale.

    0 = black, ~1 = saturated. The percentile ignores stray hot pixels; the
    full-scale normalisation makes one threshold work across exposures and
    bit depths. For confocal stacks only, NOT raw Airyscan (dim before ZEN
    reconstruction, see DEV_NOTES).

    Args:
        channel: Channel index (default 0 = DAPI).

    Returns:
        The signal level (≥ 0), or ``None`` if the file cannot be read.
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
    """True if a CZI captured essentially nothing (dark / failed-DF stack).

    ``signal_level_from_czi(...) < frac``. Skip such a site: Otsu on noise
    invents spurious detections.

    Returns:
        ``True`` / ``False``, or ``None`` if the file cannot be read.
    """
    level = signal_level_from_czi(czi_path, channel=channel)
    if level is None:
        return None
    return level < frac
