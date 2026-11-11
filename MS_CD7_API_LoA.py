# -*- coding: utf-8 -*-

#################################################################
# File        : MS_CD7_API_LoA.py
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
MS_CD7_API_LoA.py  --  the synchronous entry point of the SmartMic library.

``import MS_CD7_API_LoA as ms``. Each function wraps async ``MS_zenapi_*`` calls
in ``asyncio.run`` and adds the checks and guards the raw calls lack.

Rules
-----
* Synchronous only (own event loop and gRPC channel per call): do not call
  from async code.
* Lengths are METRES, except where a name or key says so (``thickness_um``).
* A bad argument raises ``ValueError`` / ``TypeError`` before any hardware call
  (range checks in ``MS_limits``).
* A call that gets no answer raises ``TimeoutError`` after 60 s (reads) or
  300 s (moves); an acquisition has no limit (``MS_zenapi_helpers``).
* A failure RAISES (DEV_NOTES "Rule: a failure raises"). The one exception:
  ``DefiniteFocusTracker.find_surface`` returns None for one failed position
  (the pipeline skips it) and raises after repeated failures.

Public functions
----------------
Start of a run
    preflight                       -- carrier, busy state, stage motion, versions;
                                       raises PreflightFailed
    is_microscope_busy              -- True while an experiment or acquisition runs
    get_running_experiment_status   -- status of that experiment, or None
    stop_experiment                 -- stop it (for a crash / Ctrl-C handler)

Focus (Z-drive, DefiniteFocus, software autofocus)
    get_current_z_position          -- Z in metres
    move_focus_to_new_z_position    -- absolute Z move, range-checked
    df_find_surface                 -- DF FindSurface with retries -> Z (m)
    df_recall_focus                 -- DF RecallFocus -> Z (m)
    DefiniteFocusTracker            -- FindSurface with an adaptive start and a
                                       fault after repeated failures
    swaf_find_focus                 -- software autofocus from a .czexp -> Z (m)

Stage
    get_current_xy_stage_position   -- [x, y] in metres
    move_stage_to_new_xy_position   -- lowers Z first; refuses with the immersion
                                       objective active
    set_stage_motion_sync           -- stage speed / acceleration in percent

Optics
    get_current_objective_and_optovar / _names -- position indices / names
    check_immersion_guard           -- API immersion types against the known slot
    set_optics_verified             -- change, then require a stable read-back

Sample carrier and tray
    get_sample_carrier_info / get_sample_carrier_name
    carrier_bottom                  -- name, material, thickness of the bottom
    check_carrier_for_df            -- the bottom DF uses, against the API value
    wait_for_df_bottom              -- wait until DF has the bottom after a load
    get_current_container / move_to_container  -- slide positions of an insert
    get_live_scan_configuration     -- what the prescan does on a tray load
    eject_tray / load_tray_and_prescan         -- guarded tray moves

Acquisition
    run_experiment_from_path        -- run a ``.czexp`` file

Checks (return ``(ok, message)``; the moves call them)
    validate_objective_number, validate_optovar_number, validate_z_position,
    validate_xy_position

Exceptions
    FocusSearchFailed (one DF / SWAF search failed), PreflightFailed,
    DefiniteFocusFault (DF failed repeatedly: stop the run), CarrierChanged (a
    DefiniteFocusFault), and
    ``MS_zenapi_stage_LM.ImmersionObjectiveActive`` from a refused XY move.

Hardware gotchas and the reasons for the guards: DEV_NOTES.md.
"""

import asyncio
import logging
import math
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, NotRequired, Optional, Tuple, TypedDict, Union

import zeiss_paths  # noqa: F401  — import FIRST (side effect: extends sys.path for zen_api)
from grpclib.exceptions import GRPCError
import MS_zenapi_focus
import MS_zenapi_experiment_methods
import MS_zenapi_objectivechanger
import MS_zenapi_stage_LM
import MS_zenapi_swaf
import MS_zenapi_sample_carrier
import MS_zenapi_live_scan
import MS_run_log
import MS_zen_logs
from MS_zenapi_helpers import set_logging


# Configuration constants
IMMERSION_OBJECTIVE_TIMEOUT = 120.0  # seconds - minimum timeout for immersion objectives
IMMERSION_RETRY_DELAY = 10.0  # seconds - delay when moving from immersion objective
OBJECTIVE_CHANGE_RETRY_DELAY = 5.0  # seconds - delay between retries
OBJECTIVE_POLL_INTERVAL = 0.5  # seconds - polling interval for objective change
MAX_IMMERSION_RETRIES = 3  # maximum retries for immersion operations

# set_optics_verified: wait after the read-back first shows the target optics.
# A cheap safe default, not a measured ZEN propagation time (see that function).
OPTICS_SETTLE_S = 5.0
OPTICS_VERIFY_TIMEOUT_S = 60.0   # give up if the read-back never settles
OPTICS_VERIFY_POLL_S = 2.0

# Tolerant moves (tolerance_m): pause before the one retry of a failed move.
MOVE_RETRY_DELAY_S = 1.0

# DefiniteFocusTracker defaults.
DF_APPROACH_MARGIN_M = 100e-6       # adaptive start: this far below the last surface
DF_MAX_CONSECUTIVE_FAILURES = 5     # failed searches in a row = hardware fault

# Re-exported from MS_limits; change limits there.
from MS_limits import (  # noqa: E402,F401
    OBJECTIVE_MIN, OBJECTIVE_MAX, OPTOVAR_MIN, OPTOVAR_MAX,
    Z_POSITION_MIN, Z_POSITION_MAX,
    X_STAGE_MIN, X_STAGE_MAX, Y_STAGE_MIN, Y_STAGE_MAX,
    validate_objective_number, validate_optovar_number,
    validate_z_position, validate_xy_position,
)
from MS_limits import validate_range as _validate_range  # noqa: E402,F401
from MS_limits import require_valid as _require_valid    # noqa: E402

DEFAULT_EXPERIMENT_OUTPUT_FOLDER = MS_run_log.DEFAULT_EXPERIMENT_OUTPUT_FOLDER


class CarrierBottom(TypedDict):
    """The bottom of the active sample carrier (``carrier_bottom``)."""
    name: str
    material: str
    thickness_um: float
    refractive_index: NotRequired[float]   # not in the DF log event


def df_find_surface(max_retries: int = 3, start_z_m: Optional[float] = None,
                    store_focus: bool = False) -> float:
    """
    Run DefiniteFocus FindSurface (with retries); return the Z-drive position in metres.

    Args:
        start_z_m: Search start Z (m). None: −300 µm, which retracts the
            objective: never with the 50x immersion objective (use
            DefiniteFocusTracker).
        store_focus: StoreFocus after a success (for df_recall_focus).

    Raises:
        FocusSearchFailed: No surface found in max_retries attempts.
        ValueError, TypeError: For a bad argument (not a failed search).
    """
    kwargs: Dict[str, Any] = {"max_retries": max_retries, "store_focus": store_focus}
    if start_z_m is not None:
        kwargs["start_z_m"] = start_z_m
    try:
        asyncio.run(MS_zenapi_focus.definite_focus_find_surface(**kwargs))
    except (ValueError, TypeError):
        raise
    except Exception as e:
        raise FocusSearchFailed("FindSurface", str(e),
                                getattr(e, "attempts_used", max_retries)) from e
    return get_current_z_position()


def df_recall_focus(max_retries: int = 3) -> float:
    """
    Run DefiniteFocus RecallFocus and return the Z in metres.

    Needs a FindSurface with ``store_focus=True`` first; after an objective
    change it does a full FindSurface (DEV_NOTES "What each DF RPC does").

    Raises:
        FocusSearchFailed: All attempts failed.
    """
    z_um, attempts = asyncio.run(MS_zenapi_focus.definite_focus_recall(max_retries))
    if z_um is None:
        raise FocusSearchFailed("RecallFocus", "no focus position returned", attempts)
    return z_um * 1e-6


def get_current_z_position() -> float:
    """Current Z-drive position in metres."""
    return asyncio.run(MS_zenapi_focus.get_current_z_focus_position())


class FocusSearchFailed(RuntimeError):
    """One focus search (DF FindSurface, DF RecallFocus or SWAF) found no focus.

    Attributes:
        method: "FindSurface", "RecallFocus" or "SWAF".
        attempts: Number of attempts that were made.
    """

    def __init__(self, method: str, message: str, attempts: int) -> None:
        super().__init__(f"{method} failed after {attempts} attempt(s): {message}")
        self.method = method
        self.attempts = attempts


class PreflightFailed(RuntimeError):
    """preflight found a reason not to start the run (see the message)."""


class DefiniteFocusFault(RuntimeError):
    """DefiniteFocus failed at too many positions in a row: treat as a hardware fault."""


class CarrierChanged(DefiniteFocusFault):
    """The carrier bottom that DF uses changed during the run, or is 0 (a load runs).

    A changed bottom silently moves the DF focus on the dry objectives
    (DEV_NOTES "Dry objectives: DF finds the plate UNDERSIDE").
    """


# Thicknesses closer than this are equal (µm): API and DF log round differently.
CARRIER_THICKNESS_TOL_UM = 0.5


def carrier_bottom() -> CarrierBottom:
    """The bottom of the active sample carrier, over the API (~10 ms).

    thickness_um is the measured value if it was measured at load.
    """
    info = get_sample_carrier_info()
    return {"name": info["name"], "material": info["material"],
            "thickness_um": float(info["thickness"]),
            "refractive_index": info["refractive_index"]}


def _carrier_difference(a: CarrierBottom, b: CarrierBottom) -> Optional[str]:
    """Text that describes how two carrier_bottom() dicts differ, or None."""
    diffs = []
    if str(a["material"]).lower() != str(b["material"]).lower():
        diffs.append(f"material {a['material']} → {b['material']}")
    if abs(a["thickness_um"] - b["thickness_um"]) > CARRIER_THICKNESS_TOL_UM:
        diffs.append(f"thickness {a['thickness_um']:.1f} → {b['thickness_um']:.1f} µm")
    if a.get("name") != b.get("name"):
        diffs.append(f"carrier '{a.get('name')}' → '{b.get('name')}'")
    return ", ".join(diffs) or None


def _df_bottom_as_carrier(df: MS_zen_logs.DfBottom, name: str) -> CarrierBottom:
    """A zen_df_bottom() dict as a carrier_bottom() (the DF log has no name: use ``name``)."""
    return {"name": name, "material": df["material"], "thickness_um": df["thickness_um"]}


def check_carrier_for_df(log: logging.Logger, expected_material: Optional[str] = None,
                         thickness_range_um: Optional[Tuple[float, float]] = None
                         ) -> Optional[CarrierBottom]:
    """Check the carrier bottom that DF uses (run start / preflight).

    Checks: API thickness > 0 (0: a tray load runs); material ==
    ``expected_material`` (case ignored) and thickness in ``thickness_range_um``
    (µm, e.g. (150, 190)) if given; the last DF value in the ZEN log is not 0
    and agrees with the API (logged only if the event rotated out). Warns if
    "measure bottom thickness" is off (thickness = template value).

    Returns:
        The carrier_bottom() dict, or None if a check failed (reason via log.error).
    """
    try:
        api = carrier_bottom()
    except Exception as e:
        log.error(f"DF carrier check: could not read the sample carrier ({e}).")
        return None
    text = (f"DF carrier: '{api['name']}', {api['material']}, "
            f"{api['thickness_um']:.1f} µm, n = {api['refractive_index']}")
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
    try:
        if not get_live_scan_configuration().get("measure_bottom_thickness", True):
            log.warning("DF carrier: 'measure bottom thickness' is off at tray load. The "
                        "thickness is the template value: make sure that it is correct.")
    except Exception as e:
        log.info(f"DF carrier: could not read the live-scan configuration ({e}).")
    return api


class DefiniteFocusTracker:
    """DefiniteFocus across many positions, on top of df_find_surface.

    The standard way to focus a series of positions.
    - Adaptive start: each search starts ``approach_margin_m`` below the last
      surface (much faster than a full −300 µm search).
    - Fallback: if that fails, retry once over the full range.
    - Fault stop: ``max_consecutive_failures`` failed searches in a row raise
      DefiniteFocusFault. One search can make up to 2 × max_retries attempts.
    - reset(): call after a slide/container change and after an objective
      change (surfaces differ between objectives; DEV_NOTES).

    Immersion: a full-range search starts at −300 µm, which retracts the
    objective and breaks the immersion bridge. With the immersion objective
    active the tracker never runs one: without a known surface it raises
    ImmersionObjectiveActive (pass ``surface_z_m``), and the fallback is skipped.

    Carrier check (``carrier_check``): before each search the carrier bottom
    is read (~10 ms); the first is recorded, and a later change of material or
    thickness, or thickness 0 (a load runs), raises CarrierChanged. reset()
    forgets the record. The full check is check_carrier_for_df (preflight).

    Args:
        approach_margin_m: adaptive start distance below the last surface (m).
        surface_z_m: known surface to start from (m), or None.
    """

    def __init__(self, approach_margin_m: float = DF_APPROACH_MARGIN_M,
                 max_consecutive_failures: int = DF_MAX_CONSECUTIVE_FAILURES,
                 max_retries: int = 3,
                 surface_z_m: Optional[float] = None,
                 carrier_check: bool = True) -> None:
        if approach_margin_m < 0:
            raise ValueError(f"approach_margin_m must be >= 0, got {approach_margin_m}")
        if max_consecutive_failures < 1:
            raise ValueError(f"max_consecutive_failures must be >= 1, got {max_consecutive_failures}")
        if max_retries < 1:
            raise ValueError(f"max_retries must be >= 1, got {max_retries}")
        self.approach_margin_m = approach_margin_m
        self.max_consecutive_failures = max_consecutive_failures
        self.max_retries = max_retries
        self.last_surface_z_m = surface_z_m
        self.consecutive_failures = 0
        self.last_message = ""   # why the last search failed ("" after a success)
        self.carrier_check = carrier_check
        self.carrier: Optional[CarrierBottom] = None

    def reset(self) -> None:
        """Forget the last surface and the recorded carrier (not the failure count)."""
        self.last_surface_z_m = None
        self.carrier = None

    def _check_carrier(self) -> None:
        """Record the carrier bottom on the first call; raise CarrierChanged later."""
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
        """Search the surface at the current XY.

        Returns:
            The surface Z in metres, or None if the search failed (skip the position).

        Raises:
            DefiniteFocusFault: on the max_consecutive_failures-th failure in a row.
            CarrierChanged: the carrier bottom changed or is 0. Nothing is moved.
            MS_zenapi_stage_LM.ImmersionObjectiveActive: no surface is known
                and the immersion objective is active.
        """
        log = set_logging()
        if self.carrier_check:
            self._check_carrier()
        if self.last_surface_z_m is None:
            start = None
            if _immersion_objective_active():
                raise MS_zenapi_stage_LM.ImmersionObjectiveActive(
                    "DefiniteFocusTracker: no known surface, and a full-range search "
                    "(start −300 µm) would break the immersion bridge — create the "
                    "tracker with surface_z_m near the expected surface.")
        else:
            start = self.last_surface_z_m - self.approach_margin_m

        try:
            z = df_find_surface(self.max_retries, start)
        except FocusSearchFailed as e:
            if start is None:
                return self._failed(e)
            if _immersion_objective_active():
                log.warning(f"DF from {start * 1e6:.1f} µm failed ({e}); no full-range "
                            f"retry with the immersion objective active.")
                return self._failed(e)
            log.warning(f"DF from {start * 1e6:.1f} µm failed ({e}) — retrying "
                        f"over the full range.")
            try:
                z = df_find_surface(self.max_retries)
            except FocusSearchFailed as e2:
                return self._failed(e2)
        self.consecutive_failures = 0
        self.last_message = ""
        self.last_surface_z_m = z
        return z

    def _failed(self, e: FocusSearchFailed) -> None:
        """Count one failed search; raise DefiniteFocusFault at the limit."""
        self.consecutive_failures += 1
        self.last_message = str(e)
        set_logging().warning(f"DF failed ({self.consecutive_failures} in a row): {e}")
        if self.consecutive_failures >= self.max_consecutive_failures:
            raise DefiniteFocusFault(
                f"DefiniteFocus failed at {self.consecutive_failures} positions in a "
                f"row (last: {e}) — treating it as a hardware fault.")
        return None


def _immersion_objective_active() -> bool:
    """True if the active objective is immersion (API type or known slot)."""
    return asyncio.run(MS_zenapi_stage_LM.immersion_objective_active())


def check_immersion_guard(log: logging.Logger) -> Optional[bool]:
    """Compare the immersion objectives the API reports with the known slot.

    The guard refuses a Z=0 lower for either source (API immersion type or
    MS_zenapi_stage_LM.IMMERSION_OBJECTIVE_POSITION). A mismatch means the
    turret or the constant changed: the guard still refuses for both, but the
    immersion timeouts and tray logic may not match. Read-only; preflight
    logs it and does not abort.

    Returns:
        True if the two agree, False if not (log.error), None if the inventory
        cannot be read (log.error).
    """
    slot = MS_zenapi_stage_LM.IMMERSION_OBJECTIVE_POSITION
    try:
        api = asyncio.run(MS_zenapi_objectivechanger.get_immersion_objective_positions())
    except Exception as e:
        log.error(f"Immersion guard: could not read the objective inventory ({e}). "
                  f"The guard then depends on slot {slot} only.")
        return None
    if api == [slot]:
        log.info(f"Immersion guard: the API reports the immersion objective in slot {slot}, "
                 f"as expected.")
        return True
    log.error(f"Immersion guard: the API reports immersion objectives in slots {api}, "
              f"but IMMERSION_OBJECTIVE_POSITION is {slot}. The turret or the constant "
              f"changed. Direct moves are refused for all of these slots; correct "
              f"MS_zenapi_stage_LM.IMMERSION_OBJECTIVE_POSITION before a run.")
    return False


def _move_with_tolerance(move: Callable[[], None], distance_to_target: Callable[[], float],
                         tolerance_m: Optional[float],
                         what: str, settle_s: float = 0.0) -> None:
    """Run move(); with a tolerance, tolerate ZEN's spurious "not reached".

    DEV_NOTES "Spurious 'not reached'". On a GRPCError the position is read
    back: within tolerance_m (m) → accepted (then settle_s, since the move's
    own settle was skipped); else one retry, and a second off-target failure
    raises. tolerance_m None → plain move(). Any other exception — notably the
    immersion guard's refusal — propagates without a retry.
    """
    if tolerance_m is None:
        move()
        return
    log = set_logging()
    for attempt in (1, 2):
        try:
            move()
            return
        except GRPCError as e:
            try:
                dist = distance_to_target()
            except Exception as read_err:
                log.warning(f"{what}: could not read the position back ({read_err}).")
                dist = None
            if dist is not None and dist <= tolerance_m:
                log.warning(f"{what}: ZEN reported {e} but the read-back is "
                            f"{dist * 1e6:.3f} µm from the target (tolerance "
                            f"{tolerance_m * 1e6:.3f} µm) — accepted.")
                time.sleep(settle_s)
                return
            if attempt == 2:
                raise
            off = "unknown" if dist is None else f"{dist * 1e6:.3f} µm"
            log.warning(f"{what} failed ({e}); read-back off by {off} — retrying once.")
            time.sleep(MOVE_RETRY_DELAY_S)


def _validate_tolerance(tolerance_m: Optional[float]) -> None:
    if tolerance_m is None:
        return
    # Non-finite rejected: inf would accept a failed move wherever it stopped.
    if (isinstance(tolerance_m, bool) or not isinstance(tolerance_m, (int, float))
            or not math.isfinite(tolerance_m) or tolerance_m < 0):
        raise ValueError(f"tolerance_m must be a finite, non-negative number (metres) "
                         f"or None, got {tolerance_m!r}")


def move_focus_to_new_z_position(new_z_pos: float,
                                 tolerance_m: Optional[float] = None) -> None:
    """
    Move the Z-drive to an absolute position in metres.

    WARNING: only the range is checked (validate_z_position). There is NO
    immersion guard: Z at/near 0 with the 50x immersion objective active
    retracts the objective and breaks the water bridge (DEV_NOTES). Callers
    must check the objective themselves before moving Z to 0.

    Args:
        tolerance_m: Tolerate ZEN's spurious "Requested position was not
            reached" (m; see _move_with_tolerance). Use a small positive value
            (e.g. 0.25e-6): the read-back never matches exactly. None: a ZEN
            error raises.

    Raises:
        ValueError: Position outside the safe range, or tolerance_m invalid.
    """
    _require_valid(validate_z_position(new_z_pos), "Z position")
    _validate_tolerance(tolerance_m)

    set_logging().info(f"Moving focus to Z = {new_z_pos * 1e6:.3f} µm")
    _move_with_tolerance(
        lambda: asyncio.run(MS_zenapi_focus.move_focus_to_new_z_position(new_z_pos)),
        lambda: abs(asyncio.run(MS_zenapi_focus.get_current_z_focus_position()) - new_z_pos),
        tolerance_m, f"Z move to {new_z_pos * 1e6:.3f} µm")


def get_current_xy_stage_position() -> List[float]:
    """Current stage position [x, y] in metres."""
    return asyncio.run(MS_zenapi_stage_LM.get_current_xy_stage_coordinates())


def move_stage_to_new_xy_position(new_x: float, new_y: float,
                                  tolerance_m: Optional[float] = None) -> None:
    """
    Move the stage to absolute XY in metres.

    Lowers the objective to Z = 0 first (collision safety) and leaves it
    there: re-focus at the new position before acquiring.

    Args:
        tolerance_m: Tolerate ZEN's spurious "Position could not be reached"
            (m, Euclidean; see _move_with_tolerance). The retry repeats the
            whole move incl. the Z=0 lower. None: a ZEN error raises.

    Raises:
        ValueError: Coordinates outside the safe range, or tolerance_m invalid.
        MS_zenapi_stage_LM.ImmersionObjectiveActive (a RuntimeError): The 50x
            immersion objective is active. Z=0 would break the water bridge,
            so the move is refused (never retried) — move via a positioned
            .czexp run by ZEN instead (DEV_NOTES).
    """
    _require_valid(validate_xy_position(new_x, new_y), "XY coordinates")
    _validate_tolerance(tolerance_m)

    def distance_to_target() -> float:
        x, y = asyncio.run(MS_zenapi_stage_LM.get_current_xy_stage_coordinates())
        return math.hypot(x - new_x, y - new_y)

    set_logging().info(f"Moving stage to X = {new_x * 1e6:.2f} µm, Y = {new_y * 1e6:.2f} µm")
    _move_with_tolerance(
        lambda: asyncio.run(MS_zenapi_stage_LM.move_stage_to_new_xy_position(new_x, new_y)),
        distance_to_target, tolerance_m,
        f"XY move to ({new_x * 1e6:.2f}, {new_y * 1e6:.2f}) µm",
        settle_s=MS_zenapi_stage_LM.STAGE_SETTLE_SECONDS)


def set_stage_motion_sync(speed_percent: Optional[float] = None,
                          acceleration_percent: Optional[float] = None,
                          ) -> Dict[str, Optional[float]]:
    """
    Set the XY-stage travel speed and acceleration (percent of max, [0, 100]).

    Device state held across calls: set once per run (preflight). For a live
    sample the acceleration is what jolts the medium. None → the
    MS_zenapi_stage_LM defaults (STAGE_TRAVEL_SPEED_PERCENT /
    STAGE_ACCELERATION_PERCENT, 100 = full).

    Returns:
        Read-back: speed_x, speed_y, acceleration_x, acceleration_y (percent).
        The speeds are None on this CD7: the gateway rejects GetSpeed (DEV_NOTES).
    """
    return asyncio.run(
        MS_zenapi_stage_LM.set_stage_motion(speed_percent, acceleration_percent)
    )


def run_experiment_from_path(czexp_path: Union[str, Path],
                             custom_folder: Optional[Path] = None,
                             custom_filename: Optional[str] = None
                             ) -> MS_zenapi_experiment_methods.ExperimentResult:
    """
    Run a ``.czexp`` file and move the ``.czi`` to ``custom_folder``.

    LOADED by path (not imported), so a per-run edited copy works.

    Args:
        custom_folder: Output folder; None → DEFAULT_EXPERIMENT_OUTPUT_FOLDER.
        custom_filename: Output base name (without .czi).

    Returns:
        {exp_result_path, experiment_id}

    Raises:
        FileNotFoundError: If czexp_path does not exist.
    """
    result = asyncio.run(MS_zenapi_experiment_methods.run_experiment_from_path(
        czexp_path=czexp_path,
        custom_image_folder=Path(custom_folder or DEFAULT_EXPERIMENT_OUTPUT_FOLDER),
        custom_filename=custom_filename,
    ))
    set_logging().info(f"Experiment results: {result}")
    return result


def swaf_find_focus(czexp_path: Union[str, Path], timeout: int = 30,
                    max_retries: int = 2) -> float:
    """
    Run software autofocus from a SWAF ``.czexp``; return the focus Z in metres.

    Args:
        timeout: Seconds ZEN may spend on one search.
        max_retries: Attempts including the first.

    Raises:
        FocusSearchFailed: All attempts failed.
        FileNotFoundError: czexp_path does not exist.
    """
    focus_um, attempts = asyncio.run(MS_zenapi_swaf.run_software_autofocus(
        czexp_path, timeout=timeout, max_retries=max_retries))
    if focus_um is None:
        raise FocusSearchFailed("SWAF", f"no focus found with {czexp_path}", attempts)
    return focus_um * 1e-6


def _change_optics(objective_nr: int, optovar_nr: int,
                   timeout_seconds: float = 30.0,
                   poll_interval: float = OBJECTIVE_POLL_INTERVAL,
                   max_retries: int = MAX_IMMERSION_RETRIES) -> bool:
    """
    Send the objective + optovar change and poll until the changer reports it.

    For set_optics_verified only. To/from the immersion objective the timeout
    is at least IMMERSION_OBJECTIVE_TIMEOUT, and ZEN's transient "has not been
    set" while the water is removed gets an extra IMMERSION_RETRY_DELAY.

    Returns:
        True when the read-back shows the target, False after max_retries timeouts.

    Raises:
        ValueError: A position is invalid, or not in the changer's inventory.
    """
    _require_valid(validate_objective_number(objective_nr), "objective number")
    _require_valid(validate_optovar_number(optovar_nr), "optovar number")
    log = set_logging()

    async def _set_and_wait() -> bool:
        # Positions -> lenses: DEV_NOTES "Optics map (this CD7)".
        current_obj, current_opt = await MS_zenapi_objectivechanger.get_current_objective_and_optovar()

        # Same source as the immersion move-guard: API type plus the known slot.
        immersion_positions = await MS_zenapi_stage_LM.immersion_objective_positions()
        is_from_immersion = current_obj in immersion_positions
        is_to_immersion = objective_nr in immersion_positions

        if is_from_immersion or is_to_immersion:
            adjusted_timeout = max(timeout_seconds, IMMERSION_OBJECTIVE_TIMEOUT)
            log.info(f"Immersion objective operation detected - using extended timeout: {adjusted_timeout}s")
        else:
            adjusted_timeout = timeout_seconds

        retry_count = 0
        while retry_count < max_retries:
            try:
                await MS_zenapi_objectivechanger.set_objective_set_optovar(objective_nr, optovar_nr)

                start_time = time.time()

                while time.time() - start_time < adjusted_timeout:
                    current_obj, current_opt = await MS_zenapi_objectivechanger.get_current_objective_and_optovar()

                    if current_obj == objective_nr and current_opt == optovar_nr:
                        return True

                    await asyncio.sleep(poll_interval)

                log.warning(f"Objective change: timeout after {adjusted_timeout}s, "
                            f"retry {retry_count + 1}/{max_retries}")
                retry_count += 1

            except (ValueError, TypeError):
                # Not transient (e.g. target not in the inventory): a retry
                # cannot fix it, and a False return would hide it.
                raise
            except Exception as e:
                log.warning(f"Exception during objective change: {e} — "
                            f"retry {retry_count + 1}/{max_retries}")

                # ZEN raises "has not been set" transiently while the water is
                # removed after leaving immersion.
                if "has not been set" in str(e) and is_from_immersion:
                    log.info("This is expected when moving from 50x immersion - hardware "
                             "needs time. Waiting for immersion removal process...")
                    await asyncio.sleep(IMMERSION_RETRY_DELAY)

                retry_count += 1
                await asyncio.sleep(OBJECTIVE_CHANGE_RETRY_DELAY)

        return False

    return asyncio.run(_set_and_wait())


def set_optics_verified(objective_nr: int, optovar_nr: int,
                        settle_s: float = OPTICS_SETTLE_S,
                        timeout_s: float = OPTICS_VERIFY_TIMEOUT_S,
                        poll_s: float = OPTICS_VERIFY_POLL_S) -> None:
    """
    Change objective + optovar, then VERIFY the read-back and SETTLE.

    Use before any acquisition after an optics change: a scan started right
    after a change can be tiled for the stale optics (mis-tiled mosaic). Polls
    the read-back until it shows the target, waits settle_s (a cheap safe
    default, not a measured ZEN time), checks again; aborts after timeout_s.

    Args:
        objective_nr: Objective position (1-4).
        optovar_nr: Optovar position (1-3).
        settle_s: Wait after the read-back first shows the target.
        timeout_s: Give up if the target is not read back and stable by then.

    Raises:
        ValueError: If a position or a timing argument is invalid.
        RuntimeError: If the optics never settle at the target.
    """
    for name, v in (("settle_s", settle_s), ("timeout_s", timeout_s), ("poll_s", poll_s)):
        if v < 0:
            raise ValueError(f"{name} must be >= 0, got {v}")
    log = set_logging()
    target = (objective_nr, optovar_nr)
    # A False return (changer timeout) is not fatal: the read-back is the check.
    if not _change_optics(objective_nr, optovar_nr):
        log.warning(f"Optics change to {target} not confirmed by the changer — "
                    f"verifying the read-back.")
    deadline = time.monotonic() + timeout_s
    while True:
        cur = tuple(get_current_objective_and_optovar())
        if cur == target:
            time.sleep(settle_s)
            cur = tuple(get_current_objective_and_optovar())
            if cur == target:
                log.info(f"Optics confirmed + settled: objective pos {objective_nr}, "
                         f"optovar pos {optovar_nr}.")
                return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Optics never settled at objective {objective_nr}/optovar "
                               f"{optovar_nr} (read-back {cur}) — aborting to avoid a "
                               f"mis-tiled scan.")
        log.info(f"Waiting for optics to reach (obj {objective_nr}, opt {optovar_nr}); "
                 f"currently {cur}...")
        time.sleep(poll_s)


def get_current_objective_and_optovar() -> Tuple[int, int]:
    """Current (objective_position, optovar_position)."""
    return asyncio.run(MS_zenapi_objectivechanger.get_current_objective_and_optovar())


def get_current_objective_and_optovar_names() -> Tuple[Tuple[str, int], Tuple[str, int]]:
    """
    Current ((objective_name, position), (optovar_name, position)).

    Names like 'Plan-Apochromat 20x/0.95', '2x Tubelens': verify optics by
    magnification/NA rather than slot number.
    """
    return asyncio.run(
        MS_zenapi_objectivechanger.get_current_objective_and_optovar_names()
    )


def get_sample_carrier_info() -> Dict[str, Any]:
    """
    The active sample carrier: name, rows, columns, material, thickness (µm),
    skirt, refractive_index (MS_zenapi_sample_carrier.get_sample_carrier_info).
    """
    return asyncio.run(MS_zenapi_sample_carrier.get_sample_carrier_info())


def get_sample_carrier_name() -> str:
    """Name of the active sample carrier."""
    return get_sample_carrier_info()["name"]


def get_current_container() -> Optional[Tuple[int, int]]:
    """
    The container the stage is in: (row, column) 0-based, or None.

    On the CD7 (3x slide insert) this ALWAYS returns None, even right after
    move_to_container: don't use it as a check (DEV_NOTES "Multi-slide carriers").
    """
    return asyncio.run(MS_zenapi_sample_carrier.get_current_container())


def get_live_scan_configuration() -> Dict[str, Any]:
    """ZEN's live-scan (tray load / prescan) configuration, keys as in
    MS_zenapi_live_scan.CONFIG_FIELDS. Read-only."""
    return asyncio.run(MS_zenapi_live_scan.get_live_scan_configuration())


# Dry 5x/0.35 to change to before an eject from the 50x immersion objective.
EJECT_DRY_OBJECTIVE = 2


def eject_tray(switch_to_dry_objective: bool = True,
               focus_tracker: Optional[DefiniteFocusTracker] = None) -> List[str]:
    """
    Move the sample-carrier tray out (LiveScanService.EjectTray).

    ZEN stops DF and retracts Z itself, so no Z move is done here (DEV_NOTES
    "Tray load / eject").

    Args:
        switch_to_dry_objective: If the 50x immersion objective is active,
            first change to EJECT_DRY_OBJECTIVE (5x, current optovar,
            set_optics_verified) — the safe procedure. False: eject on the 50x.
        focus_tracker: Reset before the eject.

    Returns:
        ZEN's notifications.

    Raises:
        RuntimeError: If an experiment runs (nothing is moved), or the change to
            the dry objective does not settle (the tray is not ejected).
    """
    log = set_logging()
    if is_microscope_busy():
        raise RuntimeError("Microscope is busy (an experiment runs) — tray not ejected.")
    if focus_tracker is not None:
        focus_tracker.reset()
    if switch_to_dry_objective:
        if _immersion_objective_active():
            _, optovar = get_current_objective_and_optovar()
            log.info(f"Eject: immersion objective active — changing to objective "
                     f"{EJECT_DRY_OBJECTIVE} (5x) first.")
            # Raises RuntimeError if the optics do not settle: then no eject.
            set_optics_verified(EJECT_DRY_OBJECTIVE, optovar)
    return asyncio.run(MS_zenapi_live_scan.eject_tray())


# After a tray load: wait until DF has a non-zero, stable bottom thickness.
LOAD_SETTLE_TIMEOUT_S = 60.0
LOAD_SETTLE_POLL_S = 1.0


def wait_for_df_bottom(timeout_s: float = LOAD_SETTLE_TIMEOUT_S,
                       poll_s: float = LOAD_SETTLE_POLL_S) -> CarrierBottom:
    """Wait until the carrier bottom for DF is set: thickness more than 0, stable.

    During a load ZEN sends thickness 0 to DF for some seconds; a DF search
    then parks on the plate underside (DEV_NOTES "Dry objectives: DF finds the
    plate UNDERSIDE"). Met when two reads in a row agree, the API thickness is
    > 0, and the DF log value (if present) is > 0 and matches the API.

    Returns:
        The carrier_bottom() dict.

    Raises:
        RuntimeError: if the condition is not met in timeout_s.
    """
    log = set_logging()
    deadline = time.monotonic() + timeout_s
    previous = None
    while True:
        now = carrier_bottom()
        ok = now["thickness_um"] > 0
        if ok:
            df = MS_zen_logs.zen_df_bottom()
            if df is not None:
                ok = df["thickness_um"] > 0 and _carrier_difference(
                    now, _df_bottom_as_carrier(df, now["name"])) is None
        if ok and previous is not None and _carrier_difference(previous, now) is None:
            log.info(f"DF carrier set: '{now['name']}', {now['material']}, "
                     f"{now['thickness_um']:.1f} µm.")
            return now
        previous = now if ok else None
        if time.monotonic() >= deadline:
            raise RuntimeError(f"DF carrier bottom not set {timeout_s:g} s after the load "
                               f"(last API value: {now}). Do not use DF.")
        time.sleep(poll_s)


def load_tray_and_prescan(focus_tracker: Optional[DefiniteFocusTracker] = None) -> List[str]:
    """
    Move the tray in and run the prescan (LiveScanService.LoadTrayAndPrescan).

    Prescan steps: get_live_scan_configuration. ZEN leaves the 5x (position 2)
    active after the load: set the objective again before you acquire. Do not
    start DF while a load runs; this waits for wait_for_df_bottom afterwards.

    Args:
        focus_tracker: Reset before the load.

    Returns:
        ZEN's notifications.

    Raises:
        RuntimeError: If an experiment runs (nothing is moved), or DF has no
            carrier thickness 60 s after the load (wait_for_df_bottom).
    """
    log = set_logging()
    if is_microscope_busy():
        raise RuntimeError("Microscope is busy (an experiment runs) — tray not loaded.")
    if focus_tracker is not None:
        focus_tracker.reset()
    notifications = asyncio.run(MS_zenapi_live_scan.load_tray_and_prescan())
    wait_for_df_bottom()
    try:
        objective, optovar = get_current_objective_and_optovar()
        z_um = get_current_z_position() * 1e6
        log.info(f"After load: objective {objective}, optovar {optovar}, Z {z_um:.1f} µm, "
                 f"carrier '{get_sample_carrier_name()}'. Set the objective before you acquire.")
    except Exception as e:
        log.warning(f"After load: could not read the objective / Z / carrier ({e}).")
    return notifications


def move_to_container(row_index: int, column_index: int,
                      focus_tracker: Optional[DefiniteFocusTracker] = None) -> None:
    """
    Move the stage (via ZEN) to a container, 0-based (slide 2 of 3 = row 0, column 1).

    Like move_stage_to_new_xy_position: lowers Z to 0 first (else ZEN restores
    the old slide's Z on the new one), Z ends near 0, and it refuses with the
    immersion objective active. Re-focus before acquiring. Do NOT verify with
    get_current_container (always None on the CD7); verify from the images.
    DEV_NOTES "Multi-slide carriers".

    Args:
        focus_tracker: Reset before the move.

    Raises:
        ValueError: If an index is not a non-negative integer.
        MS_zenapi_stage_LM.ImmersionObjectiveActive (a RuntimeError): If the
            immersion objective is active (nothing is moved).
    """
    for name, v in (("row_index", row_index), ("column_index", column_index)):
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(f"{name} must be a non-negative integer, got {v!r}")
    # Reset before the move: safe even if the move fails half-way.
    if focus_tracker is not None:
        focus_tracker.reset()
    asyncio.run(MS_zenapi_sample_carrier.move_to_container(row_index, column_index))


def get_running_experiment_status() -> Optional[Dict[str, Any]]:
    """
    Status of the active experiment, or None if idle.

    Keys: is_experiment_running, is_acquisition_running, images_acquired_index,
    images_count, scenes_index, scenes_count. Covers ZEN / API activity
    (experiment, snap, live, continuous), not e.g. a manual stage move.
    """
    return asyncio.run(MS_zenapi_experiment_methods.get_running_experiment_status())


def stop_experiment() -> bool:
    """
    Stop the experiment that is active in ZEN, if there is one.

    Use in a crash / Ctrl-C handler so ZEN does not continue an orphaned
    acquisition::

        try:
            run_pipeline()
        except (Exception, KeyboardInterrupt):
            log.exception("Run aborted")
            try:
                ms.stop_experiment()
            except Exception:
                log.exception("Could not stop the experiment")
            raise

    Stops the acquisition only: stage, Z and objective stay where they are
    (MS_zenapi_experiment_methods.stop_running_experiment).

    Returns:
        True if an experiment was active and the stop was sent, False if idle.

    Raises:
        GRPCError: ZEN refused the status query or the stop request.
    """
    return asyncio.run(MS_zenapi_experiment_methods.stop_running_experiment())


def is_microscope_busy() -> bool:
    """True while an experiment or acquisition runs (see get_running_experiment_status)."""
    status = get_running_experiment_status()
    if status is None:
        return False
    return bool(status["is_experiment_running"] or status["is_acquisition_running"])


def _fail(log: logging.Logger, msg: str) -> None:
    """Log a preflight failure and raise PreflightFailed with the same text."""
    log.error(msg)
    raise PreflightFailed(f"Preflight failed: {msg}")


def preflight(expected_carrier: str, log: logging.Logger,
              stage_speed_percent: Optional[float] = None,
              stage_acceleration_percent: Optional[float] = None,
              expected_material: Optional[str] = None,
              thickness_range_um: Optional[Tuple[float, float]] = None) -> None:
    """
    Standard start-of-run preflight — run this before ANY hardware action.

    Checks, in order: gateway responsive (the carrier query is the ping);
    microscope idle; ``expected_carrier`` loaded; the DF carrier bottom
    (check_carrier_for_df with expected_material / thickness_range_um, µm);
    then sets stage speed + acceleration once for the run (set_stage_motion_sync)
    and logs the read-back. Logs only, never aborts on: versions, auto-immersion
    refill settings, live-scan configuration, check_immersion_guard.

    Args:
        log: Logger for all reporting (lands in the per-run log).
        stage_speed_percent, stage_acceleration_percent: percent of max; None →
            the MS_zenapi_stage_LM defaults (100 = full). Lower for a live sample.
            If one is given and cannot be applied, preflight fails; if both are
            None, a rejected setting only warns.

    Raises:
        PreflightFailed: A check failed (the reason is also logged with
            ``log.error``). Nothing has moved.
    """
    # 0. Versions (the gRPC API has no version service: read out-of-band).
    sm_ver = zeiss_paths.smartmic_version()
    zen_ver = MS_zen_logs.zen_app_version()
    gw_ver = zeiss_paths.zen_api_gateway_version()
    api_ver, api_path = zeiss_paths.zen_api_version()
    log.info(f"SmartMic: {sm_ver or 'unknown (not a git checkout?)'}  |  "
             f"ZEN app: {zen_ver or 'unknown'}  |  "
             f"ZenApiGateway: {gw_ver or 'unknown'}  |  "
             f"zen_api stubs: {api_ver or 'unversioned (loose layout)'} @ {api_path}")

    # 0b. Auto-immersion refill settings, log only (not in the API: DEV_NOTES
    # "The re-immersion settings are NOT reachable through the ZEN-API").
    try:
        log.info(MS_zen_logs.format_auto_immersion_settings(
            MS_zen_logs.zen_auto_immersion_settings()))
    except Exception as e:
        log.info(f"Auto-immersion refill settings: could not read ({e})")

    # 1. Gateway ping.
    try:
        carrier = get_sample_carrier_name()
    except Exception as e:
        _fail(log, f"ZEN-API gateway not responding ({e}) — is ZEN running? Aborting.")

    # 1b. Live-scan configuration, log only; flags a template / carrier
    # mismatch (DEV_NOTES "Tray load / eject").
    try:
        log.info(MS_zenapi_live_scan.format_live_scan_configuration(
            get_live_scan_configuration(), carrier=carrier))
    except Exception as e:
        log.info(f"Live-scan config: could not read ({e})")

    # 1c. Immersion guard sources agree? Log only; the guard refuses for both.
    check_immersion_guard(log)

    # 2. Microscope idle.
    try:
        busy = is_microscope_busy()
    except Exception as e:
        _fail(log, f"Could not query microscope status ({e}) — aborting.")
    if busy:
        _fail(log, "Microscope is busy (an experiment is already running) — aborting.")

    # 3. Correct carrier.
    if carrier != expected_carrier:
        _fail(log, f"Wrong sample carrier: '{carrier}' "
                  f"(expected '{expected_carrier}') — aborting.")

    # 3b. DF carrier bottom: a wrong value silently moves the DF focus (dry objectives).
    if check_carrier_for_df(log, expected_material, thickness_range_um) is None:
        _fail(log, "DF carrier check failed — aborting.")

    # 4. Stage speed + acceleration. A requested throttle that cannot be applied
    # aborts (the run would silently go at full speed); with none requested a
    # rejection only warns. set_stage_motion already tolerates the gateway's
    # SetSpeed rejection (DEV_NOTES), so an exception here means ACCELERATION
    # could not be set.
    throttle_requested = (stage_speed_percent is not None
                          or stage_acceleration_percent is not None)
    motion = None
    try:
        motion = set_stage_motion_sync(stage_speed_percent, stage_acceleration_percent)
    except Exception as e:
        if throttle_requested:
            _fail(log, f"Could not set requested stage acceleration ({e}) — aborting.")
        log.warning(f"Stage acceleration not set ({e}); the device may not support "
                    f"this knob. No throttle was requested, so proceeding at the stage's "
                    f"current motion settings (full speed).")

    if motion is None:
        motion_str = "stage motion left as-is (set not supported)"
    elif motion["speed_x"] is None:
        motion_str = (f"acceleration=({motion['acceleration_x']}, {motion['acceleration_y']})% "
                       f"(speed throttle not supported by this device — acceleration only)")
    else:
        motion_str = (f"stage speed=({motion['speed_x']}, {motion['speed_y']})% "
                      f"acceleration=({motion['acceleration_x']}, {motion['acceleration_y']})%")
    log.info(f"Preflight OK: gateway responsive, microscope idle, carrier '{carrier}', {motion_str}.")
