# -*- coding: utf-8 -*-

#################################################################
# File        : MS_CD7_API_LoA.py
# Author      : Mike Stebler
# Institution : ETH Zurich | ScopeM
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

import asyncio
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any, Union


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

# Global variable for the default experiment output folder
DEFAULT_EXPERIMENT_OUTPUT_FOLDER = Path("F:/UserData/mike/api")

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
        success, attempts = asyncio.run(
            MS_zenapi_focus.definite_focus_find_surface(**kwargs)
        )
        if success:
            return True, "Definite focus successful", attempts
        else:
            return False, "Definite focus failed after retries", attempts
    except Exception as e:
        return False, f"Definite focus failed with exception: {e}", max_retries


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


def validate_objective_number(objective_nr: int) -> Tuple[bool, str]:
    """
    Validate objective number is within valid range.
    
    Args:
        objective_nr (int): Objective position number
        
    Returns:
        tuple: (is_valid: bool, message: str)
    """
    if not isinstance(objective_nr, int):
        return False, f"Objective number must be integer, got {type(objective_nr)}"
    if objective_nr < OBJECTIVE_MIN or objective_nr > OBJECTIVE_MAX:
        return False, f"Objective number must be between {OBJECTIVE_MIN}-{OBJECTIVE_MAX}, got {objective_nr}"
    return True, "Valid objective number"

def validate_optovar_number(optovar_nr: int) -> Tuple[bool, str]:
    """
    Validate optovar number is within valid range.
    
    Args:
        optovar_nr (int): Optovar position number
        
    Returns:
        tuple: (is_valid: bool, message: str)
    """
    if not isinstance(optovar_nr, int):
        return False, f"Optovar number must be integer, got {type(optovar_nr)}"
    if optovar_nr < OPTOVAR_MIN or optovar_nr > OPTOVAR_MAX:
        return False, f"Optovar number must be between {OPTOVAR_MIN}-{OPTOVAR_MAX}, got {optovar_nr}"
    return True, "Valid optovar number"

def validate_z_position(z_pos: float) -> Tuple[bool, str]:
    """
    Validate Z position is within safe limits (in meters).
    
    Args:
        z_pos (float): Z position in meters
        
    Returns:
        tuple: (is_valid: bool, message: str)
    """
    if not isinstance(z_pos, (int, float)):
        return False, f"Z position must be numeric, got {type(z_pos)}"
    if z_pos < Z_POSITION_MIN or z_pos > Z_POSITION_MAX:
        return False, f"Z position {z_pos}m outside safe range [{Z_POSITION_MIN}, {Z_POSITION_MAX}]m"
    return True, "Valid Z position"

def validate_xy_position(x: float, y: float) -> Tuple[bool, str]:
    """
    Validate XY positions are within safe hardware limits (in meters).
    
    Args:
        x (float): X coordinate in meters
        y (float): Y coordinate in meters
    
    Returns:
        tuple: (is_valid: bool, message: str)
    """
    # Validate X coordinate
    if not isinstance(x, (int, float)):
        return False, f"X coordinate must be numeric, got {type(x)}"
    if x < X_STAGE_MIN or x > X_STAGE_MAX:
        return False, f"X coordinate {x}m outside safe range [{X_STAGE_MIN}, {X_STAGE_MAX}]m"
    
    # Validate Y coordinate
    if not isinstance(y, (int, float)):
        return False, f"Y coordinate must be numeric, got {type(y)}"
    if y < Y_STAGE_MIN or y > Y_STAGE_MAX:
        return False, f"Y coordinate {y}m outside safe range [{Y_STAGE_MIN}, {Y_STAGE_MAX}]m"
    
    return True, "Valid XY coordinates within stage limits"

def move_focus_to_new_z_position(new_z_pos: float) -> None:
    """
    Move z-drive to new position (in meters).
    
    WARNING: This function moves the objective. Use with extreme caution!
    Always validate position before calling.
    
    Args:
        new_z_pos (float): New Z position in meters
        
    Raises:
        ValueError: If position is outside valid range
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
    
    This will lower the objective to z = 0 before moving the stage.
    The objective will stay at z = 0
    
    WARNING: Always validate coordinates before calling.
    
    Args:
        new_x (float): X coordinate in meters
        new_y (float): Y coordinate in meters
    
    Raises:
        ValueError: If coordinates are outside valid range
    """
    # Validate inputs using hardware-specific limits
    is_valid, message = validate_xy_position(new_x, new_y)
    if not is_valid:
        error_msg = f"Invalid XY coordinates: {message}"
        print(f"[ERROR] {error_msg}")
        raise ValueError(error_msg)
    
    print(f"[INFO] Moving stage to X = {new_x}m, Y = {new_y}m")
    asyncio.run(MS_zenapi_stage_LM.move_stage_to_new_xy_position(new_x, new_y))


def run_experiment(experiment_name: str, 
                   custom_folder: Optional[Path] = None, 
                   custom_filename: Optional[str] = None, 
                   do_snap_and_live: bool = False) -> Dict[str, Any]:
    """
    Run an experiment with the specified parameters.
    
    Args:
        experiment_name (str): Name of the experiment to run
        custom_folder (Path, optional): Custom output folder. Uses DEFAULT_EXPERIMENT_OUTPUT_FOLDER if None.
        custom_filename (str, optional): Custom filename for output
        do_snap_and_live (bool): Whether to do snap and live operations
        
    Returns:
        Dict[str, Any]: Experiment results
        
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

    result = asyncio.run(MS_zenapi_experiment_methods.check_experiment_api(
        experiment_name=experiment_name,
        custom_image_folder=custom_folder,
        custom_filename=custom_filename,
        do_snap_and_live=do_snap_and_live
    ))
    print(f"Experiment results: {result}")
    return result


def run_experiment_from_path(czexp_path: Union[str, Path],
                             custom_folder: Optional[Path] = None,
                             custom_filename: Optional[str] = None) -> Dict[str, Any]:
    """
    Run an experiment from a .czexp file path (instead of by name).

    Reads the .czexp file, imports its XML into ZEN via ExperimentService.Import,
    and runs the imported experiment.  The file does NOT need to live in ZEN's
    experiment folder — any path works, including a per-run modified experiment
    generated on the fly.

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
        # MS: This changes the objective and the optovar
        # Objective: Plan-Apochromat 20x/0.7 - Position: 1
        # Objective: Plan-Apochromat 5x/0.35 - Position: 2
        # Objective: Plan-Apochromat 20x/0.95 - Position: 3
        # Objective: Plan-Apochromat 50x/1.2 - Position: 4
        # Optovar: 2x Tubelens - Position: 1
        # Optovar: 1x Tubelens - Position: 2
        # Optovar: 0.5x Tubelens - Position: 3
        
        # Get current position to detect immersion operations
        current_obj, current_opt = await MS_zenapi_objectivechanger.get_current_objective_and_optovar()
        
        # Check if moving FROM 50x immersion objective
        is_from_immersion = current_obj == 4
        
        # Check if moving TO 50x immersion objective
        is_to_immersion = objective_nr == 4
        
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
                import time
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
                
                # Special handling for "position not set" error when moving from immersion
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










