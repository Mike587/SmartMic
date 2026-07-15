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
import time
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any, Union

import zeiss_paths  # noqa: F401  — import FIRST (side effect: extends sys.path for zen_api)
import MS_zenapi_focus
import MS_zenapi_experiment_methods
import MS_zenapi_objectivechanger
import MS_zenapi_stage_LM
import MS_zenapi_swaf
import MS_zenapi_sample_carrier
import MS_Helper_function


# Configuration constants
IMMERSION_OBJECTIVE_TIMEOUT = 120.0  # seconds - minimum timeout for immersion objectives
IMMERSION_RETRY_DELAY = 10.0  # seconds - delay when moving from immersion objective
OBJECTIVE_CHANGE_RETRY_DELAY = 5.0  # seconds - delay between retries
OBJECTIVE_POLL_INTERVAL = 0.5  # seconds - polling interval for objective change
MAX_IMMERSION_RETRIES = 3  # maximum retries for immersion operations

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

def move_focus_to_new_z_position(new_z_pos: float) -> None:
    """
    Move z-drive to new position (in meters).

    WARNING: This moves the physical Z-drive/objective — the position is
    validated internally (see Raises) but there is no further safety check
    on what a "valid" Z actually means for the current sample/objective.

    Args:
        new_z_pos (float): New Z position in meters

    Raises:
        ValueError: If position is outside the safe range (validated
            internally via validate_z_position — no need to call it yourself).
    """
    # Validate input
    is_valid, message = validate_z_position(new_z_pos)
    if not is_valid:
        error_msg = f"Invalid Z position: {message}"
        print(f"[ERROR] {error_msg}")
        raise ValueError(error_msg)
    
    print(f"[INFO] Moving focus to Z = {new_z_pos}m")
    asyncio.run(MS_zenapi_focus.move_focus_to_new_z_position(new_z_pos))


def get_current_xy_stage_position() -> List[float]:
    """
    Get current stage XY coordinates.
    
    Returns:
        List[float]: [x, y] coordinates in meters
    """
    xy = asyncio.run(MS_zenapi_stage_LM.get_current_xy_stage_coordinates())
    return xy


def move_stage_to_new_xy_position(new_x: float, new_y: float) -> None:
    """
    Move stage to absolute coordinates (in meters).

    This lowers the objective to Z = 0 before moving the stage (collision
    safety) and leaves it there — it does NOT restore the prior Z. Callers
    must re-establish focus at the new position before acquiring.

    Args:
        new_x (float): X coordinate in meters
        new_y (float): Y coordinate in meters

    Raises:
        ValueError: If coordinates are outside the safe range (validated
            internally via validate_xy_position — no need to call it yourself).
        RuntimeError: If the 50x immersion objective is active. Lowering Z to
            0 would break the immersion water bridge on this inverted CD7, so
            the move is refused rather than attempted — drive the stage via a
            positioned .czexp run by ZEN instead (see
            MS_zenapi_stage_LM.move_stage_to_new_xy_position / DEV_NOTES.md).
    """
    # Validate inputs using hardware-specific limits
    is_valid, message = validate_xy_position(new_x, new_y)
    if not is_valid:
        error_msg = f"Invalid XY coordinates: {message}"
        print(f"[ERROR] {error_msg}")
        raise ValueError(error_msg)
    
    print(f"[INFO] Moving stage to X = {new_x}m, Y = {new_y}m")
    asyncio.run(MS_zenapi_stage_LM.move_stage_to_new_xy_position(new_x, new_y))


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










