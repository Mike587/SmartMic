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

import asyncio
import math
import time
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any, Union

import zeiss_paths  # noqa: F401  — import FIRST (side effect: extends sys.path for zen_api)
from grpclib.exceptions import GRPCError
import MS_zenapi_focus
import MS_zenapi_experiment_methods
import MS_zenapi_objectivechanger
import MS_zenapi_stage_LM
import MS_zenapi_swaf
import MS_zenapi_sample_carrier
import MS_Helper_function
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

# Hardware limits
OBJECTIVE_MIN = 1
OBJECTIVE_MAX = 4
OPTOVAR_MIN = 1
OPTOVAR_MAX = 3
Z_POSITION_MIN = -0.01  # meters
Z_POSITION_MAX = 0.01   # meters
X_STAGE_MIN = 0.0       # meters
X_STAGE_MAX = 0.12      # meters
Y_STAGE_MIN = 0.0       # meters
Y_STAGE_MAX = 0.09      # meters

# Default experiment output folder — single-sourced from MS_Helper_function
# so the literal path lives in exactly one place.
DEFAULT_EXPERIMENT_OUTPUT_FOLDER = MS_Helper_function.DEFAULT_EXPERIMENT_OUTPUT_FOLDER

# NOTE: All functions in this module wrap async hardware API calls in synchronous
# helpers. Each call creates its own event loop — do not mix with top-level async code.

def run_definite_focus_find_surface(max_retries: int = 3,
                                    start_z_m: Optional[float] = None) -> Tuple[bool, str, int]:
    """
    Run definite focus find surface with retry logic.

    Args:
        max_retries (int):          Maximum number of retry attempts (default: 3).
        start_z_m   (float|None):   Z position in metres to move to before searching.
                                    None → use the async function's default (−300 µm).
                                    Pass last_surface_z − 100e-6 to speed up subsequent DFs.

    Returns:
        Tuple[bool, str, int]: (success, message, attempts_used)
    """
    kwargs = {"max_retries": max_retries}
    if start_z_m is not None:
        kwargs["start_z_m"] = start_z_m
    try:
        # definite_focus_find_surface only ever returns on success — it raises
        # (with attempts_used attached) after exhausting max_retries — so there
        # is no separate "clean" False outcome to branch on here.
        success, attempts = asyncio.run(
            MS_zenapi_focus.definite_focus_find_surface(**kwargs)
        )
        return True, "Definite focus successful", attempts
    except Exception as e:
        attempts_used = getattr(e, "attempts_used", max_retries)
        return False, f"Definite focus failed with exception: {e}", attempts_used


def run_definite_focus_recall(max_retries: int = 3) -> Tuple[Optional[float], int]:
    """
    Run DefiniteFocus RecallFocus — snap back to the last stored focus position.

    This is a fast operation (~1 s) that requires a previous FindSurface +
    StoreFocus to have been executed first.

    Returns:
        Tuple (z_um, attempts_used).  z_um is None if all attempts fail.
    """
    return asyncio.run(MS_zenapi_focus.definite_focus_recall(max_retries))


def get_current_z_position() -> float:
    """
    Get current z-drive position.
    
    Returns:
        float: Current Z position in meters
    """
    current_z_position = asyncio.run(MS_zenapi_focus.get_current_z_focus_position())
    return current_z_position


class DefiniteFocusFault(RuntimeError):
    """DefiniteFocus failed at too many positions in a row: treat as a hardware fault."""


class DefiniteFocusTracker:
    """DefiniteFocus across many positions, on top of run_definite_focus_find_surface.

    - Adaptive start: each search starts ``approach_margin_m`` below the last
      surface found (~4 s on the CD7, vs ~11 s for a full search from the
      −300 µm default). Use this as the standard way to focus a series of
      positions.
    - Fallback: if the adaptive search fails (surface lower than expected:
      slide tilt, next well), retry once over the full range.
    - Hardware-fault stop: after ``max_consecutive_failures`` failed searches
      in a row, raise DefiniteFocusFault instead of carrying on out of focus
      (2026-09-28: DF dropped out at 22:14 and the run went on for 2 h). One
      search = one find_surface call; each can make up to 2 × max_retries
      FindSurface attempts (adaptive + fallback).
    - reset(): forget the last surface. Call it after a slide/container change
      (move_to_container does it when given the tracker) and after an objective
      change — surfaces differ between objectives (DEV_NOTES).

    Immersion: a full-range search starts at −300 µm, which retracts the
    objective and breaks the immersion bridge (and fails at 50x anyway —
    DEV_NOTES). With the immersion objective active the tracker never runs one:
    without a known surface it raises ImmersionObjectiveActive (pass
    ``surface_z_m`` near the expected surface), and the fallback is skipped.

    Args:
        approach_margin_m: adaptive start distance below the last surface (m).
        max_consecutive_failures: failed searches in a row before the stop.
        max_retries: FindSurface attempts per search (run_definite_focus_find_surface).
        surface_z_m: known surface to start from (e.g. from an earlier run), or None.
    """

    def __init__(self, approach_margin_m: float = DF_APPROACH_MARGIN_M,
                 max_consecutive_failures: int = DF_MAX_CONSECUTIVE_FAILURES,
                 max_retries: int = 3,
                 surface_z_m: Optional[float] = None):
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
        self.last_message = ""
        self.last_attempts = 0

    def reset(self) -> None:
        """Forget the last surface (the failure count is kept: a slide change
        does not fix a DefiniteFocus fault)."""
        self.last_surface_z_m = None

    def find_surface(self) -> Optional[float]:
        """Search the surface at the current XY.

        Returns:
            The surface Z in metres (drive read-back), or None if the search
            failed (the position should be skipped).

        Raises:
            DefiniteFocusFault: on the max_consecutive_failures-th failure in a row.
            MS_zenapi_stage_LM.ImmersionObjectiveActive: if no surface is known
                and the immersion objective is active (see the class docstring).
        """
        log = set_logging()
        if self.last_surface_z_m is None:
            start = None
            if _immersion_objective_active():
                raise MS_zenapi_stage_LM.ImmersionObjectiveActive(
                    "DefiniteFocusTracker: no known surface, and a full-range search "
                    "(start −300 µm) would break the immersion bridge — create the "
                    "tracker with surface_z_m near the expected surface.")
        else:
            start = self.last_surface_z_m - self.approach_margin_m

        ok, msg, attempts = run_definite_focus_find_surface(self.max_retries, start)
        total_attempts = attempts
        if not ok and start is not None:
            if _immersion_objective_active():
                log.warning(f"DF from {start * 1e6:.1f} µm failed ({msg}); no full-range "
                            f"retry with the immersion objective active.")
            else:
                log.warning(f"DF from {start * 1e6:.1f} µm failed ({msg}) — retrying "
                            f"over the full range.")
                ok, msg, attempts = run_definite_focus_find_surface(self.max_retries)
                total_attempts += attempts
        self.last_message, self.last_attempts = msg, total_attempts

        if not ok:
            self.consecutive_failures += 1
            log.warning(f"DF failed ({self.consecutive_failures} in a row): {msg}")
            if self.consecutive_failures >= self.max_consecutive_failures:
                raise DefiniteFocusFault(
                    f"DefiniteFocus failed at {self.consecutive_failures} positions in a "
                    f"row (last: {msg}) — treating it as a hardware fault.")
            return None

        self.consecutive_failures = 0
        z = get_current_z_position()
        self.last_surface_z_m = z
        return z


def _immersion_objective_active() -> bool:
    obj, _ = get_current_objective_and_optovar()
    return obj == MS_zenapi_stage_LM.IMMERSION_OBJECTIVE_POSITION


def _move_with_tolerance(move, distance_to_target, tolerance_m: Optional[float],
                         what: str, settle_s: float = 0.0) -> None:
    """Run move(); with a tolerance, tolerate ZEN's spurious "not reached".

    ZEN sometimes reports a move as failed (gRPC INTERNAL "Requested position
    was not reached." for Z, "Position could not be reached." for XY) although
    the drive is on target — DEV_NOTES "Spurious 'not reached'". On such an
    error, read the position back: within tolerance_m → done (warning logged,
    then settle_s); otherwise retry the move once. A second failure that is
    still off target raises.

    Only ZEN-reported errors (GRPCError) are handled; anything else — notably
    the immersion guard's refusal — propagates untouched, without a retry.

    Args:
        move: performs the move (raises on failure).
        distance_to_target: returns the read-back distance to the target (m).
        tolerance_m: accepted read-back distance (m); None → plain move().
        what: description for the log.
        settle_s: wait after accepting a move that raised (the move's own settle
            wait is skipped when it raises).
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
    # Non-finite is rejected too: an infinite tolerance would accept any failed
    # move wherever the drive stopped.
    if (isinstance(tolerance_m, bool) or not isinstance(tolerance_m, (int, float))
            or not math.isfinite(tolerance_m) or tolerance_m < 0):
        raise ValueError(f"tolerance_m must be a finite, non-negative number (metres) "
                         f"or None, got {tolerance_m!r}")


def _validate_range(value, lo, hi, label: str, type_check, type_name: str,
                     unit: str = "") -> Tuple[bool, str]:
    """Shared isinstance + min/max + (bool, message) check.

    Backs validate_objective_number / validate_optovar_number /
    validate_z_position / validate_xy_position below, which differed only in
    bounds, the expected type, and label text.
    """
    if not isinstance(value, type_check):
        return False, f"{label} must be {type_name}, got {type(value)}"
    if value < lo or value > hi:
        return False, f"{label} {value}{unit} outside safe range [{lo}, {hi}]{unit}"
    return True, f"Valid {label.lower()}"


def validate_objective_number(objective_nr: int) -> Tuple[bool, str]:
    """
    Validate objective number is within valid range.

    Args:
        objective_nr (int): Objective position number

    Returns:
        tuple: (is_valid: bool, message: str)
    """
    return _validate_range(objective_nr, OBJECTIVE_MIN, OBJECTIVE_MAX,
                            "Objective number", int, "integer")

def validate_optovar_number(optovar_nr: int) -> Tuple[bool, str]:
    """
    Validate optovar number is within valid range.

    Args:
        optovar_nr (int): Optovar position number

    Returns:
        tuple: (is_valid: bool, message: str)
    """
    return _validate_range(optovar_nr, OPTOVAR_MIN, OPTOVAR_MAX,
                            "Optovar number", int, "integer")

def validate_z_position(z_pos: float) -> Tuple[bool, str]:
    """
    Validate Z position is within safe limits (in meters).

    Args:
        z_pos (float): Z position in meters

    Returns:
        tuple: (is_valid: bool, message: str)
    """
    return _validate_range(z_pos, Z_POSITION_MIN, Z_POSITION_MAX,
                            "Z position", (int, float), "numeric", unit="m")

def validate_xy_position(x: float, y: float) -> Tuple[bool, str]:
    """
    Validate XY positions are within safe hardware limits (in meters).

    Args:
        x (float): X coordinate in meters
        y (float): Y coordinate in meters

    Returns:
        tuple: (is_valid: bool, message: str)
    """
    x_ok, x_msg = _validate_range(x, X_STAGE_MIN, X_STAGE_MAX,
                                   "X coordinate", (int, float), "numeric", unit="m")
    if not x_ok:
        return x_ok, x_msg

    y_ok, y_msg = _validate_range(y, Y_STAGE_MIN, Y_STAGE_MAX,
                                   "Y coordinate", (int, float), "numeric", unit="m")
    if not y_ok:
        return y_ok, y_msg

    return True, "Valid XY coordinates within stage limits"

def move_focus_to_new_z_position(new_z_pos: float,
                                 tolerance_m: Optional[float] = None) -> None:
    """
    Move z-drive to new position (in meters).

    WARNING: This moves the physical Z-drive/objective — the position is
    validated internally (see Raises) but there is no further safety check
    on what a "valid" Z actually means for the current sample/objective.
    Unlike move_stage_to_new_xy_position, this function has NO immersion
    guard: calling it with new_z_pos at/near 0 while the 50x immersion
    objective is active will retract the objective and break the immersion
    water bridge (see DEV_NOTES.md). Callers must check the current
    objective themselves before moving Z to 0 with immersion active.

    Args:
        new_z_pos (float): New Z position in meters
        tolerance_m (float, optional): Tolerate ZEN's spurious "Requested
            position was not reached" (~0.7 % of focus moves on 2026-09-28,
            drive on target). On a ZEN error, Z is read back: within
            tolerance_m of the target → done; otherwise one retry. The
            read-back never matches the target exactly, so pick a small
            positive value (Marc_SM_002: 0.25e-6). None (default) → no
            tolerance, a ZEN error raises as before.

    Raises:
        ValueError: If position is outside the safe range (validated
            internally via validate_z_position — no need to call it yourself),
            or tolerance_m is invalid.
    """
    # Validate input
    is_valid, message = validate_z_position(new_z_pos)
    if not is_valid:
        error_msg = f"Invalid Z position: {message}"
        print(f"[ERROR] {error_msg}")
        raise ValueError(error_msg)
    _validate_tolerance(tolerance_m)

    print(f"[INFO] Moving focus to Z = {new_z_pos}m")
    _move_with_tolerance(
        lambda: asyncio.run(MS_zenapi_focus.move_focus_to_new_z_position(new_z_pos)),
        lambda: abs(asyncio.run(MS_zenapi_focus.get_current_z_focus_position()) - new_z_pos),
        tolerance_m, f"Z move to {new_z_pos * 1e6:.3f} µm")


def get_current_xy_stage_position() -> List[float]:
    """
    Get current stage XY coordinates.
    
    Returns:
        List[float]: [x, y] coordinates in meters
    """
    xy = asyncio.run(MS_zenapi_stage_LM.get_current_xy_stage_coordinates())
    return xy


def move_stage_to_new_xy_position(new_x: float, new_y: float,
                                  tolerance_m: Optional[float] = None) -> None:
    """
    Move stage to absolute coordinates (in meters).

    This lowers the objective to Z = 0 before moving the stage (collision
    safety) and leaves it there — it does NOT restore the prior Z. Callers
    must re-establish focus at the new position before acquiring.

    Args:
        new_x (float): X coordinate in meters
        new_y (float): Y coordinate in meters
        tolerance_m (float, optional): Tolerate ZEN's spurious "Position could
            not be reached" (11 of ~1600 moves on 2026-09-29/30, all tiny
            2–10 µm hops). On a ZEN error, XY is read back: within tolerance_m
            (Euclidean distance) of the target → done; otherwise one retry
            (the whole move again, incl. the Z=0 lower). None (default) → no
            tolerance, a ZEN error raises as before.

    Raises:
        ValueError: If coordinates are outside the safe range (validated
            internally via validate_xy_position — no need to call it yourself),
            or tolerance_m is invalid.
        MS_zenapi_stage_LM.ImmersionObjectiveActive (a RuntimeError): If the
            50x immersion objective is active. Lowering Z to 0 would break the
            immersion water bridge on this inverted CD7, so the move is refused
            rather than attempted (never retried) — drive the stage via a
            positioned .czexp run by ZEN instead (see
            MS_zenapi_stage_LM.move_stage_to_new_xy_position / DEV_NOTES.md).
    """
    # Validate inputs using hardware-specific limits
    is_valid, message = validate_xy_position(new_x, new_y)
    if not is_valid:
        error_msg = f"Invalid XY coordinates: {message}"
        print(f"[ERROR] {error_msg}")
        raise ValueError(error_msg)
    _validate_tolerance(tolerance_m)

    def distance_to_target():
        x, y = asyncio.run(MS_zenapi_stage_LM.get_current_xy_stage_coordinates())
        return math.hypot(x - new_x, y - new_y)

    print(f"[INFO] Moving stage to X = {new_x}m, Y = {new_y}m")
    _move_with_tolerance(
        lambda: asyncio.run(MS_zenapi_stage_LM.move_stage_to_new_xy_position(new_x, new_y)),
        distance_to_target, tolerance_m,
        f"XY move to ({new_x * 1e6:.2f}, {new_y * 1e6:.2f}) µm",
        settle_s=MS_zenapi_stage_LM.STAGE_SETTLE_SECONDS)


def set_stage_motion_sync(speed_percent: Optional[float] = None,
                          acceleration_percent: Optional[float] = None) -> Dict[str, float]:
    """
    Set the XY-stage travel speed and acceleration (percent of max, [0, 100]).

    Speed/acceleration are device state, not per-move parameters: the controller
    holds them across SmartMic's per-call channel open/close, so this is the
    once-per-run "global stage-speed" knob (set it in preflight and leave it).
    Both are set together; for a live sample it is the acceleration (jerk) that
    disturbs the medium, so a low speed with a high acceleration would still jolt
    it. None falls back to the module defaults in MS_zenapi_stage_LM
    (STAGE_TRAVEL_SPEED_PERCENT / STAGE_ACCELERATION_PERCENT, both 100 = full).

    Args:
        speed_percent (float, optional): XY travel speed, percent of max.
        acceleration_percent (float, optional): XY acceleration, percent of max.

    Returns:
        Dict[str, float]: read-back after setting, with keys speed_x, speed_y,
        acceleration_x, acceleration_y (percent).
    """
    return asyncio.run(
        MS_zenapi_stage_LM.set_stage_motion(speed_percent, acceleration_percent)
    )


def run_experiment(experiment_name: str, 
                   custom_folder: Optional[Path] = None, 
                   custom_filename: Optional[str] = None, 
                   do_snap_and_live: bool = False) -> Dict[str, Any]:
    """
    Run an experiment by name with the specified parameters.

    By default this uses the lean acquisition path
    (``run_experiment_by_name``): load the named experiment, run it, collect the
    result. When ``do_snap_and_live=True`` it instead routes to
    ``check_experiment_api`` — the full API smoke test that also exercises
    clone/export/import/delete and snap/live/continuous. Normal pipeline
    acquisitions should leave ``do_snap_and_live=False``.

    Args:
        experiment_name (str): Name of the experiment to run
        custom_folder (Path, optional): Custom output folder. Uses DEFAULT_EXPERIMENT_OUTPUT_FOLDER if None.
        custom_filename (str, optional): Custom filename for output
        do_snap_and_live (bool): When True, run the full smoke-test path
            (snap + live + continuous + serialization round-trip) instead of a
            plain acquisition.

    Returns:
        Dict[str, Any]: Experiment results ({exp_result_path, snap_path,
        experiment_id}).

    Raises:
        ValueError: If experiment_name is empty or invalid
    """
    # Validate experiment name
    if not experiment_name or not isinstance(experiment_name, str):
        raise ValueError(f"Invalid experiment name: {experiment_name}")

    # If custom_folder is not provided, use the global default
    if custom_folder is None:
        custom_folder = DEFAULT_EXPERIMENT_OUTPUT_FOLDER

    # Ensure custom_folder is a Path object
    custom_folder = Path(custom_folder)

    if do_snap_and_live:
        # Full API exercise incl. snap/live/continuous + serialization round-trip.
        result = asyncio.run(MS_zenapi_experiment_methods.check_experiment_api(
            experiment_name=experiment_name,
            custom_image_folder=custom_folder,
            custom_filename=custom_filename,
            do_snap_and_live=True,
        ))
    else:
        # Lean production acquisition: load by name -> run -> collect.
        result = asyncio.run(MS_zenapi_experiment_methods.run_experiment_by_name(
            experiment_name=experiment_name,
            custom_image_folder=custom_folder,
            custom_filename=custom_filename,
        ))
    print(f"Experiment results: {result}")
    return result


def run_experiment_from_path(czexp_path: Union[str, Path],
                             custom_folder: Optional[Path] = None,
                             custom_filename: Optional[str] = None) -> Dict[str, Any]:
    """
    Run an experiment from a .czexp file path (instead of by name).

    LOADS the experiment via ExperimentService.Load (which accepts a full path,
    not just a name in ZEN's experiment folder) and runs the loaded id — so any
    path works, including a per-run modified experiment generated on the fly. It
    does NOT import the XML and run the imported id: an imported experiment is not
    runnable and ZEN throws at run for tile/region experiments (use
    run_experiment_from_xml for the Import path).

    Args:
        czexp_path (str|Path):           Path to the .czexp file to run.
        custom_folder (Path, optional):  Output folder. Uses
                                         DEFAULT_EXPERIMENT_OUTPUT_FOLDER if None.
        custom_filename (str, optional): Output base filename (without .czi).

    Returns:
        Dict[str, Any]: {exp_result_path, snap_path, experiment_id}

    Raises:
        FileNotFoundError: If czexp_path does not exist.
    """
    czexp_path = Path(czexp_path)
    if not czexp_path.exists():
        raise FileNotFoundError(f"Experiment file not found: {czexp_path}")

    if custom_folder is None:
        custom_folder = DEFAULT_EXPERIMENT_OUTPUT_FOLDER
    custom_folder = Path(custom_folder)

    result = asyncio.run(MS_zenapi_experiment_methods.run_experiment_from_path(
        czexp_path=czexp_path,
        custom_image_folder=custom_folder,
        custom_filename=custom_filename,
    ))
    print(f"Experiment results: {result}")
    return result


def run_experiment_from_xml(xml: str,
                            custom_folder: Optional[Path] = None,
                            custom_filename: Optional[str] = None) -> Dict[str, Any]:
    """
    Run an experiment from an XML string (instead of a name or file path).

    Imports the experiment XML into ZEN via ExperimentService.Import and runs
    it.  Accepts a raw <HardwareExperiment> string (e.g. built on the fly) or
    full .czexp content — a leading BOM / <?xml ...?> prolog is normalized away.
    Use this to run a per-spheroid experiment without writing it to disk.

    Args:
        xml (str):                       The experiment XML string.
        custom_folder (Path, optional):  Output folder. Uses
                                         DEFAULT_EXPERIMENT_OUTPUT_FOLDER if None.
        custom_filename (str, optional): Output base filename (without .czi).

    Returns:
        Dict[str, Any]: {exp_result_path, snap_path, experiment_id}
    """
    if custom_folder is None:
        custom_folder = DEFAULT_EXPERIMENT_OUTPUT_FOLDER
    custom_folder = Path(custom_folder)

    result = asyncio.run(MS_zenapi_experiment_methods.run_experiment_from_xml(
        xml=xml,
        custom_image_folder=custom_folder,
        custom_filename=custom_filename,
    ))
    print(f"Experiment results: {result}")
    return result


def run_swaf(swaf_experiment_name: str, timeout: int = 30) -> Tuple[Optional[float], int]:
    """
    Run software autofocus (SWAF) using the named experiment.

    Args:
        swaf_experiment_name: Name of the SWAF experiment (without .czexp extension).
        timeout:              Search timeout in seconds (default 30).

    Returns:
        Tuple (focus_pos_um, attempts_used).
        focus_pos_um is None if all attempts failed.
    """
    return asyncio.run(MS_zenapi_swaf.run_software_autofocus(swaf_experiment_name, timeout=timeout))


def run_swaf_from_path(czexp_path: Union[str, Path], timeout: int = 30) -> Tuple[Optional[float], int]:
    """
    Run software autofocus (SWAF) from a standalone ``.czexp`` file path.

    The portable counterpart to :func:`run_swaf`: the SWAF experiment is loaded
    by path rather than by a name that must already exist in ZEN's library, so
    the call is self-contained (used by the test suite and any caller that ships
    its own experiment files).

    Args:
        czexp_path: Path to the SWAF ``.czexp`` file to load and run.
        timeout:    Search timeout in seconds (default 30).

    Returns:
        Tuple (focus_pos_um, attempts_used).
        focus_pos_um is None if all attempts failed.
    """
    return asyncio.run(MS_zenapi_swaf.run_software_autofocus_from_path(czexp_path, timeout=timeout))


def set_objective_set_optovar_sync(objective_nr, optovar_nr, timeout_seconds=30.0, 
                                   poll_interval=OBJECTIVE_POLL_INTERVAL, 
                                   max_retries=MAX_IMMERSION_RETRIES):
    """
    Change the objective and optovar, and wait for the hardware to complete the change.
    
    Args:
        objective_nr (int): Objective position (1-4)
        optovar_nr (int): Optovar position (1-3)
        timeout_seconds (float): Maximum time to wait for change to complete
        poll_interval (float): Time between polls of current position
        max_retries (int): Maximum number of retry attempts for immersion objectives
    
    Returns:
        bool: True if change completed successfully, False if timeout
    
    Raises:
        ValueError: If objective_nr or optovar_nr are invalid
    """
    # Validate inputs
    obj_valid, obj_msg = validate_objective_number(objective_nr)
    if not obj_valid:
        raise ValueError(f"Invalid objective number: {obj_msg}")
    
    opt_valid, opt_msg = validate_optovar_number(optovar_nr)
    if not opt_valid:
        raise ValueError(f"Invalid optovar number: {opt_msg}")
    
    async def _set_and_wait():
        # See DEV_NOTES.md "Optics map (this CD7)" for the objective/optovar
        # position -> lens/tubelens mapping (not repeated here to avoid a
        # second copy that can drift if the turret is ever reconfigured).

        # Get current position to detect immersion operations
        current_obj, current_opt = await MS_zenapi_objectivechanger.get_current_objective_and_optovar()
        
        # Check if moving FROM/TO the 50x immersion objective. Shares the
        # single source of truth (MS_zenapi_stage_LM.IMMERSION_OBJECTIVE_POSITION)
        # with the immersion move-guard, instead of a second hardcoded literal
        # that could silently drift out of sync with it.
        is_from_immersion = current_obj == MS_zenapi_stage_LM.IMMERSION_OBJECTIVE_POSITION
        is_to_immersion = objective_nr == MS_zenapi_stage_LM.IMMERSION_OBJECTIVE_POSITION
        
        # Adjust timeout for immersion operations
        if is_from_immersion or is_to_immersion:
            # Much longer timeout for immersion operations
            adjusted_timeout = max(timeout_seconds, IMMERSION_OBJECTIVE_TIMEOUT)
            print(f"[INFO] Immersion objective operation detected - using extended timeout: {adjusted_timeout}s")
        else:
            adjusted_timeout = timeout_seconds
        
        retry_count = 0
        while retry_count < max_retries:
            try:
                # Send the command to change objective and optovar
                await MS_zenapi_objectivechanger.set_objective_set_optovar(objective_nr, optovar_nr)
                
                # Wait for the hardware to complete the change
                start_time = time.time()
                
                while time.time() - start_time < adjusted_timeout:
                    # Get current objective and optovar
                    current_obj, current_opt = await MS_zenapi_objectivechanger.get_current_objective_and_optovar()
                    
                    # Check if both match the desired values
                    if current_obj == objective_nr and current_opt == optovar_nr:
                        return True
                    
                    # Wait before polling again
                    await asyncio.sleep(poll_interval)
                
                # Timeout reached
                print(f"[WARN] Timeout after {adjusted_timeout}s, retry {retry_count + 1}/{max_retries}")
                retry_count += 1
                
            except Exception as e:
                print(f"[WARN] Exception during objective change: {e}")
                print(f"[INFO] Retry {retry_count + 1}/{max_retries}")
                
                # Special handling for the "has not been set" error ZEN raises
                # transiently when moving from immersion (hardware needs time
                # to complete the immersion-removal process).
                if "has not been set" in str(e) and is_from_immersion:
                    print("[INFO] This is expected when moving from 50x immersion - hardware needs time")
                    print("[INFO] Waiting for immersion removal process...")
                    await asyncio.sleep(IMMERSION_RETRY_DELAY)  # Wait for immersion removal
                
                retry_count += 1
                await asyncio.sleep(OBJECTIVE_CHANGE_RETRY_DELAY)  # Wait before retry
        
        # All retries failed
        return False

    return asyncio.run(_set_and_wait())


def set_optics_verified(objective_nr: int, optovar_nr: int,
                        settle_s: float = OPTICS_SETTLE_S,
                        timeout_s: float = OPTICS_VERIFY_TIMEOUT_S,
                        poll_s: float = OPTICS_VERIFY_POLL_S) -> None:
    """
    Change objective + optovar, then VERIFY the read-back and SETTLE.

    Use before any acquisition after an optics change. ZEN builds the tile grid
    from the CURRENT optics at run time; a scan launched right after the change
    was once tiled for the stale config (HD_Nuclei_from_slide, run_20260629_122403,
    leaving the 50x immersion objective: doubled/ghosted mosaic). Steps:
      1. change the optics (set_objective_set_optovar_sync);
      2. poll the read-back until it shows the target;
      3. wait settle_s;
      4. check again — abort if it never settles within timeout_s.

    The cause (read-back leading ZEN's acquisition engine) is inferred and was
    only seen when leaving the immersion objective, so the settle is a cheap safe
    default, not confirmed ZEN behaviour. See TODO "pin an experiment to a
    DEFINED objective/optovar config" for a possible ZEN-native fix.

    Args:
        objective_nr (int): Objective position (1-4).
        optovar_nr (int): Optovar position (1-3).
        settle_s (float): Wait after the read-back first shows the target.
        timeout_s (float): Give up if the target is not read back and stable by then.
        poll_s (float): Interval between read-backs while waiting.

    Raises:
        ValueError: If a position or a timing argument is invalid.
        RuntimeError: If the optics never settle at the target.
    """
    for name, v in (("settle_s", settle_s), ("timeout_s", timeout_s), ("poll_s", poll_s)):
        if v < 0:
            raise ValueError(f"{name} must be >= 0, got {v}")
    log = set_logging()
    target = (objective_nr, optovar_nr)
    # Validates the positions (ValueError). A False return (changer timeout) is
    # not fatal by itself: the read-back below is the check.
    if not set_objective_set_optovar_sync(objective_nr, optovar_nr):
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
    """
    Get current objective and optovar positions.

    Returns:
        Tuple[int, int]: (objective_position, optovar_position)
    """
    return asyncio.run(MS_zenapi_objectivechanger.get_current_objective_and_optovar())


def get_current_objective_and_optovar_names() -> Tuple[Tuple[str, int], Tuple[str, int]]:
    """
    Get current objective and optovar as (name, position) pairs.

    Resolves the human-readable hardware names (e.g. 'Plan-Apochromat 20x/0.95'
    and '2x Tubelens') so callers can verify the optics by magnification/NA
    rather than by slot number.

    Returns:
        Tuple[Tuple[str, int], Tuple[str, int]]:
            ((objective_name, objective_position), (optovar_name, optovar_position))
    """
    return asyncio.run(
        MS_zenapi_objectivechanger.get_current_objective_and_optovar_names()
    )


def get_sample_carrier_info() -> Dict[str, Any]:
    """
    Get information about the currently configured sample carrier.

    Returns:
        Dict[str, Any] with keys: name, rows, columns, material,
        thickness, skirt, refractive_index. See
        MS_zenapi_sample_carrier.get_sample_carrier_info for details.
    """
    return asyncio.run(MS_zenapi_sample_carrier.get_sample_carrier_info())


def get_sample_carrier_name() -> str:
    """
    Get the name of the currently configured sample carrier.

    Returns:
        str: The sample-carrier name as reported by ZEN.
    """
    return get_sample_carrier_info()["name"]


def get_current_container() -> Optional[Tuple[int, int]]:
    """
    Get the sample-carrier container the stage is positioned in.

    Returns:
        (row_index, column_index), 0-based, or None if the stage is in no
        container. Read-only.

    Note:
        On the CD7 (3x slide insert) this ALWAYS returns None (ZEN: NOT_FOUND),
        even right after move_to_container — don't use it as a check
        (DEV_NOTES "Multi-slide carriers").
    """
    return asyncio.run(MS_zenapi_sample_carrier.get_current_container())


def move_to_container(row_index: int, column_index: int,
                      focus_tracker: Optional[DefiniteFocusTracker] = None) -> None:
    """
    Move the stage to a sample-carrier container (e.g. slide 2 of a 3-slide
    insert = row 0, column 1; indices 0-based). The move is executed by ZEN.

    Behaves like move_stage_to_new_xy_position: lowers Z to 0 first, so Z ends
    near 0 (4.59 µm seen) on the new slide, and refuses with the immersion
    objective active. (ZEN retracts Z during the switch, then restores the Z it
    had when called — without the lowering that would be the old slide's
    height on the new slide.) Re-focus before acquiring. Verified on the scope
    2026-10-14 (DEV_NOTES "Multi-slide carriers").

    Do NOT verify the move with get_current_container: it always answers
    NOT_FOUND on the CD7 (DEV_NOTES "Multi-slide carriers"). Verify the slide
    from the images instead.

    Args:
        row_index (int), column_index (int): The container, 0-based.
        focus_tracker (DefiniteFocusTracker, optional): Reset before the move —
            the last surface belongs to the previous slide.

    Raises:
        ValueError: If an index is not a non-negative integer.
        MS_zenapi_stage_LM.ImmersionObjectiveActive (a RuntimeError): If the
            immersion objective is active (nothing is moved).
    """
    for name, v in (("row_index", row_index), ("column_index", column_index)):
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(f"{name} must be a non-negative integer, got {v!r}")
    # Before the move: forgetting the surface is always safe, also if the move
    # then fails half-way.
    if focus_tracker is not None:
        focus_tracker.reset()
    asyncio.run(MS_zenapi_sample_carrier.move_to_container(row_index, column_index))


def get_running_experiment_status() -> Optional[Dict[str, Any]]:
    """
    Get the status of the currently active experiment, if any.

    Returns:
        Dict[str, Any] with keys is_experiment_running, is_acquisition_running,
        images_acquired_index, images_count, scenes_index, scenes_count — or
        None if the microscope is idle (no experiment running).

    Note:
        Reflects activity started through ZEN / the API (standard experiment,
        snap, live, continuous). It does not report unrelated activity such as
        a manual stage move outside an experiment.
    """
    return asyncio.run(MS_zenapi_experiment_methods.get_running_experiment_status())


def is_microscope_busy() -> bool:
    """
    Check whether the microscope is currently running an experiment/acquisition.

    Returns:
        bool: True if an experiment or acquisition is running, False if idle.

    Note:
        See get_running_experiment_status for what "busy" covers.
    """
    status = get_running_experiment_status()
    if status is None:
        return False
    return bool(status["is_experiment_running"] or status["is_acquisition_running"])


def preflight(expected_carrier: str, log,
              stage_speed_percent: Optional[float] = None,
              stage_acceleration_percent: Optional[float] = None) -> bool:
    """
    Standard start-of-run preflight — run this before ANY hardware action.

    Checks, in order:
      1. the ZEN-API gateway is responsive (a live carrier query that doubles as
         a ping),
      2. the microscope is idle (no experiment already running),
      3. the correct sample carrier is loaded,
      4. sets the XY-stage travel speed + acceleration once for the run (device
         state the controller holds across calls) and logs the read-back.

    Args:
        expected_carrier (str): The sample-carrier name the run requires; the
            run aborts if a different carrier is loaded.
        log: A logger (anything with .info/.error). Used for all reporting so
            the outcome lands in the per-run log file.
        stage_speed_percent (float, optional): XY travel speed, percent of max.
            None → STAGE_TRAVEL_SPEED_PERCENT (100 = full speed). Turn down for a
            sensitive/live sample.
        stage_acceleration_percent (float, optional): XY acceleration, percent of
            max. None → STAGE_ACCELERATION_PERCENT (100 = full).

    Returns:
        bool: True if all checks pass and stage motion was set; False otherwise
        (the reason is logged via ``log.error``). Callers should abort on False.
    """
    # 0. Record version identity: none of this is available over the gRPC API
    # itself (no version/about service), so each piece is recovered out-of-band
    # (see zeiss_paths.zen_app_version / zen_api_gateway_version / zen_api_version /
    # smartmic_version) and logged together so a support question ("what were you
    # running?") has a complete answer: the ZEN application, the gateway service it
    # talks to, the Python-side stubs generated against it, AND the exact SmartMic
    # commit driving the run.
    sm_ver = zeiss_paths.smartmic_version()
    zen_ver = zeiss_paths.zen_app_version()
    gw_ver = zeiss_paths.zen_api_gateway_version()
    api_ver, api_path = zeiss_paths.zen_api_version()
    log.info(f"SmartMic: {sm_ver or 'unknown (not a git checkout?)'}  |  "
             f"ZEN app: {zen_ver or 'unknown'}  |  "
             f"ZenApiGateway: {gw_ver or 'unknown'}  |  "
             f"zen_api stubs: {api_ver or 'unversioned (loose layout)'} @ {api_path}")

    # 1. Gateway ping (the carrier query is also the liveness check).
    try:
        carrier = get_sample_carrier_name()
    except Exception as e:
        log.error(f"ZEN-API gateway not responding ({e}) — is ZEN running? Aborting.")
        return False

    # 2. Microscope idle.
    try:
        if is_microscope_busy():
            log.error("Microscope is busy (an experiment is already running) — aborting.")
            return False
    except Exception as e:
        log.error(f"Could not query microscope status ({e}) — aborting.")
        return False

    # 3. Correct carrier.
    if carrier != expected_carrier:
        log.error(f"Wrong sample carrier: '{carrier}' "
                  f"(expected '{expected_carrier}') — aborting.")
        return False

    # 4. Stage speed + acceleration (set once for the run).
    #
    # Whether a failure here is fatal depends on INTENT. If the caller explicitly
    # asked to throttle (speed/accel not None) — e.g. a sensitive/live sample —
    # and we cannot apply it, abort: proceeding at an unknown (likely full) speed
    # would silently violate the intended protection. But if no throttle was
    # requested (both None → full speed, no protection intended), a device that
    # rejects the knob outright must NOT block the run — we weren't limiting
    # anything anyway. Warn and proceed at the stage's current motion settings.
    #
    # set_stage_motion already tolerates a speed-only failure internally (this
    # gateway rejects SetSpeed/GetSpeed outright, confirmed 2026-07-14, but
    # SetAcceleration/GetAcceleration work fine — see MS_zenapi_stage_LM), so an
    # exception here means ACCELERATION itself — the parameter that actually
    # matters for protecting a live sample — could not be applied.
    throttle_requested = (stage_speed_percent is not None
                          or stage_acceleration_percent is not None)
    motion = None
    try:
        motion = set_stage_motion_sync(stage_speed_percent, stage_acceleration_percent)
    except Exception as e:
        if throttle_requested:
            log.error(f"Could not set requested stage acceleration ({e}) — aborting.")
            return False
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
    return True










