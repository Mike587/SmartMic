# -*- coding: utf-8 -*-

#################################################################
# File        : MS_definite_focus.py
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
DefiniteFocus (DF) for a sequence of positions, and the carrier bottom that DF uses.

``DefiniteFocusTracker`` is the usual way to focus many positions. DF needs
the correct material and thickness of the carrier bottom. A wrong value moves
the DF focus on a dry objective, and it gives no message (DEV_NOTES "Dry
objectives: DF finds the plate UNDERSIDE"). Thus this module also holds the
checks of that value: ``check_carrier_for_df`` for the start of a run,
``wait_for_df_bottom`` for the time after a tray load, and the check that the
tracker does before each search.

Use these names through ``MS_CD7_API_LoA`` (``import MS_CD7_API_LoA as ms``).
This module calls the device functions as ``MS_CD7_devices.<name>``.
"""

import logging
import time
from typing import NotRequired, Optional, Tuple, TypedDict

import zeiss_paths  # noqa: F401  — import FIRST (side effect: extends sys.path for zen_api)
import MS_CD7_devices
import MS_zen_logs
from MS_logging import set_logging

# The default values of DefiniteFocusTracker.
DF_APPROACH_MARGIN_M = 100e-6       # A search starts this far below the last surface.
DF_MAX_CONSECUTIVE_FAILURES = 5     # This many failures in sequence is a hardware fault.

# Two thicknesses in µm that are nearer to each other than this value are
# equal. The API and the DF log round the value in different ways.
CARRIER_THICKNESS_TOL_UM = 0.5

# After a tray load, wait until DF has a stable bottom thickness that is more
# than 0.
LOAD_SETTLE_TIMEOUT_S = 60.0
LOAD_SETTLE_POLL_S = 1.0


# ---------------------------------------------------------------------------
# The types and the exceptions
# ---------------------------------------------------------------------------

class CarrierBottom(TypedDict):
    """The bottom of the active sample carrier. See ``carrier_bottom``."""
    name: str
    material: str
    thickness_um: float
    refractive_index: NotRequired[float]   # The DF log event does not hold this.


class DefiniteFocusFault(RuntimeError):
    """DefiniteFocus failed at too many positions in sequence.

    Treat this condition as a fault of the hardware.
    """


class CarrierChanged(DefiniteFocusFault):
    """The carrier bottom that DF uses changed during the run, or it is 0.

    A value of 0 means that a tray load runs. A bottom that changes moves the
    DF focus on a dry objective, and it gives no message (DEV_NOTES "Dry
    objectives: DF finds the plate UNDERSIDE").
    """


# ---------------------------------------------------------------------------
# The carrier bottom that DefiniteFocus uses
# ---------------------------------------------------------------------------

def carrier_bottom() -> CarrierBottom:
    """Get the bottom of the active sample carrier from the API.

    The call needs approximately 10 ms. ``thickness_um`` is the measured
    value, if ZEN measured it at the load.
    """
    info = MS_CD7_devices.get_sample_carrier_info()
    return {"name": info["name"], "material": info["material"],
            "thickness_um": float(info["thickness"]),
            "refractive_index": info["refractive_index"]}


def _carrier_difference(a: CarrierBottom, b: CarrierBottom) -> Optional[str]:
    """Make a text that tells how two ``carrier_bottom()`` dicts are different.

    The result is ``None`` if the two dicts are the same.
    """
    diffs = []
    if str(a["material"]).lower() != str(b["material"]).lower():
        diffs.append(f"material {a['material']} → {b['material']}")
    if abs(a["thickness_um"] - b["thickness_um"]) > CARRIER_THICKNESS_TOL_UM:
        diffs.append(f"thickness {a['thickness_um']:.1f} → {b['thickness_um']:.1f} µm")
    if a.get("name") != b.get("name"):
        diffs.append(f"carrier '{a.get('name')}' → '{b.get('name')}'")
    return ", ".join(diffs) or None


def _df_bottom_as_carrier(df: MS_zen_logs.DfBottom, name: str) -> CarrierBottom:
    """Change a ``zen_df_bottom()`` dict into a ``carrier_bottom()`` dict.

    The DF log holds no name. Thus the function uses ``name``.
    """
    return {"name": name, "material": df["material"], "thickness_um": df["thickness_um"]}


def check_carrier_for_df(log: logging.Logger, expected_material: Optional[str] = None,
                         thickness_range_um: Optional[Tuple[float, float]] = None
                         ) -> Optional[CarrierBottom]:
    """Check the carrier bottom that DF uses, at a run start or in preflight.

    The function does these checks. The thickness from the API must be more
    than 0, because 0 means that a tray load runs. If you give
    ``expected_material``, the material must be equal to it, and the comparison
    ignores the letter case. If you give ``thickness_range_um`` in µm, for
    example (150, 190), the thickness must be in that range. The last DF value
    in the ZEN log must not be 0, and it must agree with the API. If ZEN
    rotated that event out, the function only writes a note to the log. The
    function gives a warning if "measure bottom thickness" is off, because the
    thickness is then the value from the template.

    Returns:
        The ``carrier_bottom()`` dict, or ``None`` if a check failed. The
        function writes the reason with ``log.error``.
    """
    try:
        api = carrier_bottom()
    except Exception as e:
        log.error(f"DF carrier check: could not read the sample carrier ({e}).")
        return None

    text =(f"DF carrier: '{api['name']}', {api['material']}, "
            f"{api['thickness_um']:.1f} µm, n = {api['refractive_index']}")

    # The value from the API.
    if api["thickness_um"] <= 0:
        log.error(f"{text} — thickness 0: a tray load runs or the carrier has no "
                  f"thickness. Do not use DF.")
        return None
    if expected_material and str(api["material"]).lower() != expected_material.lower():
        log.error(f"{text} — material is not {expected_material}. On a dry objective DF "
                  f"then focuses far from the surface (DEV_NOTES).")
        return None
    if thickness_range_um is not None:
        lo, hi = thickness_range_um
        if not lo <= api["thickness_um"] <= hi:
            log.error(f"{text} — thickness is not in {lo:g}…{hi:g} µm. Measure it at load "
                      f"or correct the carrier template.")
            return None

    # The value that DF received, from the ZEN log.
    try:
        df = MS_zen_logs.zen_df_bottom()
    except Exception as e:
        df = None
        log.info(f"DF carrier: could not read the ZEN log ({e}).")
    if df is None:
        log.info(f"{text}. The DF value is not in the ZEN logs (rotated): API value only.")
    else:
        if df["thickness_um"] <= 0:
            log.error(f"{text} — but DF has thickness 0 (ZEN log {df['timestamp']}): a tray "
                      f"load runs. Do not use DF.")
            return None
        diff = _carrier_difference(api, _df_bottom_as_carrier(df, api["name"]))
        if diff:
            log.error(f"{text} — DF has a different value (ZEN log {df['timestamp']}: {diff}).")
            return None
        log.info(f"{text}; DF has the same value (ZEN log {df['timestamp']}).")

    # The source of the thickness: a measurement at the load, or the template.
    try:
        live_scan = MS_CD7_devices.get_live_scan_configuration()
        if not live_scan.get("measure_bottom_thickness", True):
            log.warning("DF carrier: 'measure bottom thickness' is off at tray load. The "
                        "thickness is the template value: make sure that it is correct.")
    except Exception as e:
        log.info(f"DF carrier: could not read the live-scan configuration ({e}).")
    return api


def _df_has_bottom(api: CarrierBottom) -> bool:
    """Return ``True`` if DF has the carrier bottom that the API reports.

    The thickness from the API must be more than 0. The value in the DF log,
    if it is there, must be more than 0 and must agree with the API.
    """
    if api["thickness_um"] <= 0:
        return False
    df = MS_zen_logs.zen_df_bottom()
    if df is None:
        return True
    return (df["thickness_um"] > 0
            and _carrier_difference(api, _df_bottom_as_carrier(df, api["name"])) is None)


def wait_for_df_bottom(timeout_s: float = LOAD_SETTLE_TIMEOUT_S,
                       poll_s: float = LOAD_SETTLE_POLL_S) -> CarrierBottom:
    """Wait until the carrier bottom for DF is correct.

    The thickness must be more than 0 and stable. During a load, ZEN sends a
    thickness of 0 to DF for some seconds. A DF search then stops on the
    underside of the plate (DEV_NOTES "Dry objectives: DF finds the plate
    UNDERSIDE"). The condition is correct when two reads in sequence agree, the
    thickness from the API is more than 0, and the value in the DF log, if it
    is there, is more than 0 and agrees with the API.

    Returns:
        The ``carrier_bottom()`` dict.

    Raises:
        RuntimeError: The condition is not correct within ``timeout_s``.
    """
    log = set_logging()
    deadline = time.monotonic() + timeout_s
    previous = None
    while True:
        now = carrier_bottom()
        ok = _df_has_bottom(now)
        if ok and previous is not None and _carrier_difference(previous, now) is None:
            log.info(f"DF carrier set: '{now['name']}', {now['material']}, "
                     f"{now['thickness_um']:.1f} µm.")
            return now
        previous = now if ok else None
        if time.monotonic() >= deadline:
            raise RuntimeError(f"DF carrier bottom not set {timeout_s:g} s after the load "
                               f"(last API value: {now}). Do not use DF.")
        time.sleep(poll_s)


# ---------------------------------------------------------------------------
# The tracker
# ---------------------------------------------------------------------------

class DefiniteFocusTracker:
    """Use DefiniteFocus at many positions, on top of ``df_find_surface``.

    This class is the usual way to focus a sequence of positions.

    - The start point adapts. Each search starts ``approach_margin_m`` below
      the last surface. This is much faster than a full search from -300 µm.
    - If that search fails, the tracker tries one time over the full range.
    - If ``max_consecutive_failures`` searches fail in sequence, the tracker
      raises DefiniteFocusFault. One search can make as many as
      2 x ``max_retries`` attempts.
    - Call ``reset()`` after a change of the slide or of the container, and
      after an objective change. The surface is different for each objective.
      See DEV_NOTES.

    Immersion: a full-range search starts at -300 µm. That move pulls the
    objective back and breaks the immersion bridge. Thus the tracker never
    makes a full-range search while the immersion objective is active. If it
    knows no surface, it raises ImmersionObjectiveActive. To prevent that, give
    ``surface_z_m``. The tracker also does not make the full-range search after
    a failure.

    The carrier check (``carrier_check``): before each search, the tracker
    reads the carrier bottom, which needs approximately 10 ms. It records the
    first value. A later change of the material or of the thickness raises
    CarrierChanged. A thickness of 0, which means that a load runs, also raises
    CarrierChanged. ``reset()`` removes the record. The full check is
    ``check_carrier_for_df``, in preflight.

    Args:
        approach_margin_m: The distance in metres below the last surface at
            which a search starts.
        surface_z_m: A known surface in metres to start from, or ``None``.
    """

    def __init__(self, approach_margin_m: float = DF_APPROACH_MARGIN_M,
                 max_consecutive_failures: int = DF_MAX_CONSECUTIVE_FAILURES,
                 max_retries: int = 3,
                 surface_z_m: Optional[float] = None,
                 carrier_check: bool = True) -> None:
        if approach_margin_m < 0:
            raise ValueError(f"approach_margin_m must be >= 0, got {approach_margin_m}")
        if max_consecutive_failures < 1:
            raise ValueError(f"max_consecutive_failures must be >= 1, "
                             f"got {max_consecutive_failures}")
        if max_retries < 1:
            raise ValueError(f"max_retries must be >= 1, got {max_retries}")
        self.approach_margin_m = approach_margin_m
        self.max_consecutive_failures = max_consecutive_failures
        self.max_retries = max_retries
        self.last_surface_z_m = surface_z_m
        self.consecutive_failures = 0
        self.last_message = ""   # The reason of the last failure. "" after a success.
        self.carrier_check = carrier_check
        self.carrier: Optional[CarrierBottom] = None

    def reset(self) -> None:
        """Remove the last surface and the carrier record. Keep the failure count."""
        self.last_surface_z_m = None
        self.carrier = None

    def _check_carrier(self) -> None:
        """Record the carrier bottom at the first call.

        At a later call, the function raises CarrierChanged if the bottom
        changed.
        """
        log = set_logging()
        now = carrier_bottom()
        if now["thickness_um"] <= 0:
            raise CarrierChanged(f"Carrier bottom thickness is 0 ('{now['name']}'): a tray "
                                 f"load runs or the carrier has no thickness. DF stopped.")
        if self.carrier is None:
            self.carrier = now
            log.info(f"DF tracker: carrier '{now['name']}', {now['material']}, "
                     f"{now['thickness_um']:.1f} µm recorded.")
            return
        diff = _carrier_difference(self.carrier, now)
        if diff:
            raise CarrierChanged(f"The carrier bottom changed during the run ({diff}). "
                                 f"DF focus would move. Stop, check the carrier, then reset "
                                 f"the tracker.")

    def find_surface(self) -> Optional[float]:
        """Search for the surface at the current XY position.

        Returns:
            The Z of the surface in metres. The result is ``None`` if the
            search failed, and the caller then goes to the next position.

        Raises:
            DefiniteFocusFault: This failure is failure number
                ``max_consecutive_failures`` in sequence.
            CarrierChanged: The carrier bottom changed, or it is 0. The
                function moves nothing.
            ImmersionObjectiveActive: The tracker knows no surface and the
                immersion objective is active.
        """
        log = set_logging()
        if self.carrier_check:
            self._check_carrier()
        if self.last_surface_z_m is None:
            start = None
            if MS_CD7_devices.immersion_objective_active():
                raise MS_CD7_devices.ImmersionObjectiveActive(
                    "DefiniteFocusTracker: no known surface, and a full-range search "
                    "(start −300 µm) would break the immersion bridge — create the "
                    "tracker with surface_z_m near the expected surface.")
        else:
            start = self.last_surface_z_m - self.approach_margin_m

        try:
            z = MS_CD7_devices.df_find_surface(self.max_retries, start)
        except MS_CD7_devices.FocusSearchFailed as e:
            if start is None:
                return self._failed(e)
            if MS_CD7_devices.immersion_objective_active():
                log.warning(f"DF from {start * 1e6:.1f} µm failed ({e}); no full-range "
                            f"retry with the immersion objective active.")
                return self._failed(e)
            log.warning(f"DF from {start * 1e6:.1f} µm failed ({e}) — retrying "
                        f"over the full range.")
            try:
                z = MS_CD7_devices.df_find_surface(self.max_retries)
            except MS_CD7_devices.FocusSearchFailed as e2:
                return self._failed(e2)
        self.consecutive_failures = 0
        self.last_message = ""
        self.last_surface_z_m = z
        return z

    def _failed(self, e: MS_CD7_devices.FocusSearchFailed) -> None:
        """Count one failed search, and raise DefiniteFocusFault at the limit."""
        self.consecutive_failures += 1
        self.last_message = str(e)
        set_logging().warning(f"DF failed ({self.consecutive_failures} in a row): {e}")
        if self.consecutive_failures >= self.max_consecutive_failures:
            raise DefiniteFocusFault(
                f"DefiniteFocus failed at {self.consecutive_failures} positions in a "
                f"row (last: {e}) — treating it as a hardware fault.")
        return None
