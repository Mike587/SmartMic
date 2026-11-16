# -*- coding: utf-8 -*-

#################################################################
# File        : MS_CD7_devices.py
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
The synchronous calls to the devices of the CD7.

Each function puts the asynchronous ``MS_zenapi_*`` calls into ``asyncio.run``,
and adds the checks that the raw calls do not have. A function in this module
uses one device, or does one call. The procedures that use more than one
device are in ``MS_definite_focus`` and ``MS_CD7_procedures``.

Use these functions through ``MS_CD7_API_LoA`` (``import MS_CD7_API_LoA as
ms``). That module gives the list of the public functions and the rules for
them.

A module above this one calls a function here as ``MS_CD7_devices.<name>``.
Thus a test can replace the function in this module, and each caller gets the
replacement.
"""

import asyncio
import math
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple, Union

import zeiss_paths  # noqa: F401  — import FIRST (side effect: extends sys.path for zen_api)
from grpclib.exceptions import GRPCError
import MS_zenapi_focus
import MS_zenapi_experiment_methods
import MS_zenapi_objectivechanger
import MS_zenapi_stage_LM
import MS_zenapi_swaf
import MS_zenapi_sample_carrier
import MS_zenapi_live_scan
import MS_limits
import MS_run_log
from MS_logging import set_logging

# The objective change, in _change_optics.
IMMERSION_OBJECTIVE_TIMEOUT = 120.0  # s. The minimum limit for an immersion objective.
IMMERSION_RETRY_DELAY = 10.0         # s. The wait after a move from immersion.
OBJECTIVE_CHANGE_RETRY_DELAY = 5.0   # s. The wait between two retries.
OBJECTIVE_POLL_INTERVAL = 0.5        # s. The interval between two read-backs.
MAX_IMMERSION_RETRIES = 3            # The maximum number of retries.

# set_optics_verified waits this time after the read-back first shows the
# target optics. This is a safe default value. It is not a measured time that
# ZEN needs. See that function.
OPTICS_SETTLE_S = 5.0
OPTICS_VERIFY_TIMEOUT_S = 60.0   # Stop if the read-back does not become stable.
OPTICS_VERIFY_POLL_S = 2.0

# For a move that has a tolerance_m, this is the wait before the one retry of
# a move that failed.
MOVE_RETRY_DELAY_S = 1.0

DEFAULT_EXPERIMENT_OUTPUT_FOLDER = MS_run_log.DEFAULT_EXPERIMENT_OUTPUT_FOLDER


# ---------------------------------------------------------------------------
# The types and the exceptions
# ---------------------------------------------------------------------------

# The exception of the immersion guard. The guard is in
# MS_zenapi_objectivechanger.
ImmersionObjectiveActive = MS_zenapi_objectivechanger.ImmersionObjectiveActive


class Resettable(Protocol):
    """An object that has a ``reset()`` method.

    An example is ``MS_definite_focus.DefiniteFocusTracker``, which is in a
    module above this one.
    """

    def reset(self) -> None:
        """Remove the state that a move makes invalid."""


class FocusSearchFailed(RuntimeError):
    """One focus search found no focus.

    The search is a DF FindSurface, a DF RecallFocus or a SWAF.

    Attributes:
        method: "FindSurface", "RecallFocus" or "SWAF".
        attempts: The number of attempts that the code made.
    """

    def __init__(self, method: str, message: str, attempts: int) -> None:
        super().__init__(f"{method} failed after {attempts} attempt(s): {message}")
        self.method = method
        self.attempts = attempts


# ---------------------------------------------------------------------------
# The status of the experiment
# ---------------------------------------------------------------------------

def is_microscope_busy() -> bool:
    """Return ``True`` while an experiment or an acquisition runs.

    See ``get_running_experiment_status``.
    """
    status = get_running_experiment_status()
    if status is None:
        return False
    return bool(status["is_experiment_running"] or status["is_acquisition_running"])


def get_running_experiment_status() -> Optional[Dict[str, Any]]:
    """
    Get the status of the active experiment, or ``None`` if the microscope is idle.

    The keys are is_experiment_running, is_acquisition_running,
    images_acquired_index, images_count, scenes_index and scenes_count. The
    status covers the work of ZEN and of the API: an experiment, a snap, a live
    view and a continuous acquisition. It does not cover a manual stage move.
    """
    return asyncio.run(MS_zenapi_experiment_methods.get_running_experiment_status())


def stop_experiment() -> bool:
    """
    Stop the experiment that is active in ZEN, if there is one.

    Use this function in a handler for a crash or for Ctrl-C. Then ZEN does not
    continue an acquisition that has no owner::

        try:
            run_pipeline()
        except (Exception, KeyboardInterrupt):
            log.exception("Run aborted")
            try:
                ms.stop_experiment()
            except Exception:
                log.exception("Could not stop the experiment")
            raise

    The function stops only the acquisition. The stage, the Z-drive and the
    objective stay where they are. See
    ``MS_zenapi_experiment_methods.stop_running_experiment``.

    Returns:
        ``True`` if an experiment was active and the function sent the stop.
        ``False`` if the microscope is idle.

    Raises:
        GRPCError: ZEN refused the status request or the stop request.
    """
    return asyncio.run(MS_zenapi_experiment_methods.stop_running_experiment())


# ---------------------------------------------------------------------------
# Moves that accept a wrong not-reached error from ZEN
# ---------------------------------------------------------------------------

def _validate_tolerance(tolerance_m: Optional[float]) -> None:
    """Raise ``ValueError`` if ``tolerance_m`` is not ``None`` or a finite number >= 0.

    A value of inf accepts a failed move at any position. Thus the function
    refuses a value that is not finite.
    """
    if tolerance_m is None:
        return
    if (isinstance(tolerance_m, bool) or not isinstance(tolerance_m, (int, float))
            or not math.isfinite(tolerance_m) or tolerance_m < 0):
        raise ValueError(f"tolerance_m must be a finite, non-negative number (metres) "
                         f"or None, got {tolerance_m!r}")


def _move_with_tolerance(move: Callable[[], None], distance_to_target: Callable[[], float],
                         tolerance_m: Optional[float],
                         what: str, settle_s: float = 0.0) -> None:
    """Run ``move()``. With a tolerance, accept a wrong "not reached" from ZEN.

    See DEV_NOTES "Spurious 'not reached'". After a GRPCError, the function
    reads the position back. If the position is within ``tolerance_m`` in
    metres, the function accepts the move. It then waits ``settle_s``, because
    the move did not do its own wait. If the position is not within the
    tolerance, the function tries the move one more time. A second failure away
    from the target raises an exception. With ``tolerance_m`` as ``None``, the
    function only calls ``move()``. Each other exception goes to the caller
    with no retry. The refusal of the immersion guard is one such exception.
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


# ---------------------------------------------------------------------------
# Focus: the Z-drive, DefiniteFocus and the software autofocus
# ---------------------------------------------------------------------------

def get_current_z_position() -> float:
    """Get the current Z-drive position in metres."""
    return asyncio.run(MS_zenapi_focus.get_current_z_focus_position())


def move_focus_to_new_z_position(new_z_pos: float,
                                 tolerance_m: Optional[float] = None) -> None:
    """
    Move the Z-drive to an absolute position in metres.

    WARNING: the function checks only the range, with ``MS_limits``.
    It has NO immersion guard. A Z of 0, or near 0, while the 50x immersion
    objective is active, pulls the objective back and breaks the water bridge
    (DEV_NOTES). The caller must examine the objective before it moves Z to 0.

    Args:
        tolerance_m: Accept a wrong "Requested position was not reached" from
            ZEN, in metres. See ``_move_with_tolerance``. Use a small value
            that is more than 0, for example 0.25e-6, because the read-back is
            never exactly equal. With ``None``, an error from ZEN raises an
            exception.

    Raises:
        ValueError: The position is outside the safe range, or ``tolerance_m``
            is not correct.
    """
    MS_limits.require_z_position(new_z_pos)
    _validate_tolerance(tolerance_m)

    set_logging().info(f"Moving focus to Z = {new_z_pos * 1e6:.3f} µm")
    _move_with_tolerance(
        lambda: asyncio.run(MS_zenapi_focus.move_focus_to_new_z_position(new_z_pos)),
        lambda: abs(asyncio.run(MS_zenapi_focus.get_current_z_focus_position()) - new_z_pos),
        tolerance_m, f"Z move to {new_z_pos * 1e6:.3f} µm")


def df_find_surface(max_retries: int = 3, start_z_m: Optional[float] = None,
                    store_focus: bool = False) -> float:
    """
    Run DefiniteFocus FindSurface with retries, and give the Z-drive position.

    The result is in metres.

    Args:
        start_z_m: The Z in metres at which the search starts. ``None`` gives
            -300 µm. That value pulls the objective back. Never use it with
            the 50x immersion objective. Use DefiniteFocusTracker in its place.
        store_focus: Run StoreFocus after a success, for ``df_recall_focus``.

    Raises:
        FocusSearchFailed: The function found no surface in ``max_retries``
            attempts.
        ValueError, TypeError: An argument is not correct. This is not a failed
            search.
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
    Run DefiniteFocus RecallFocus and give the Z in metres.

    This function needs a FindSurface with ``store_focus=True`` before it.
    After an objective change, RecallFocus does a full FindSurface (DEV_NOTES
    "What each DF RPC does").

    Raises:
        FocusSearchFailed: All the attempts failed.
    """
    z_um, attempts = asyncio.run(MS_zenapi_focus.definite_focus_recall(max_retries))
    if z_um is None:
        raise FocusSearchFailed("RecallFocus", "no focus position returned", attempts)
    return z_um * 1e-6


def swaf_find_focus(czexp_path: Union[str, Path], timeout: int = 30,
                    max_retries: int = 2) -> float:
    """
    Run the software autofocus from a SWAF ``.czexp`` file.

    Args:
        timeout: The number of seconds that ZEN can use for one search.
        max_retries: The number of attempts, including the first attempt.

    Returns:
        The focus Z in metres.

    Raises:
        FocusSearchFailed: All the attempts failed.
        FileNotFoundError: ``czexp_path`` does not exist.
    """
    focus_um, attempts = asyncio.run(MS_zenapi_swaf.run_software_autofocus(
        czexp_path, timeout=timeout, max_retries=max_retries))
    if focus_um is None:
        raise FocusSearchFailed("SWAF", f"no focus found with {czexp_path}", attempts)
    return focus_um * 1e-6


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------

def get_current_xy_stage_position() -> List[float]:
    """Get the current stage position ``[x, y]`` in metres."""
    return asyncio.run(MS_zenapi_stage_LM.get_current_xy_stage_coordinates())


def move_stage_to_new_xy_position(new_x: float, new_y: float,
                                  tolerance_m: Optional[float] = None) -> None:
    """
    Move the stage to an absolute XY position in metres.

    The function lowers the objective to Z = 0 first, to prevent a collision,
    and it leaves the objective there. Focus again at the new position before
    you acquire.

    Args:
        tolerance_m: Accept a wrong "Position could not be reached" from ZEN,
            in metres. The function measures the straight-line distance. See
            ``_move_with_tolerance``. The retry does the full move again,
            including the move of Z to 0. With ``None``, an error from ZEN
            raises an exception.

    Raises:
        ValueError: A coordinate is outside the safe range, or ``tolerance_m``
            is not correct.
        ImmersionObjectiveActive (a RuntimeError): The 50x immersion
            objective is active. A Z of 0 breaks the water bridge.
            Thus the function refuses the move, and it never tries the move
            again. Write the target position into a ``.czexp`` file and let ZEN
            run it (DEV_NOTES).
    """
    MS_limits.require_xy_position(new_x, new_y)
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
    Set the travel speed and the acceleration of the XY stage.

    Both values are a percent of the maximum, in the range [0, 100]. They are
    device state, and the device keeps them between calls. Thus you set them
    one time for each run, in preflight. With a live sample, the acceleration
    moves the medium, not the speed. ``None`` gives the default values of
    MS_zenapi_stage_LM: STAGE_TRAVEL_SPEED_PERCENT and
    STAGE_ACCELERATION_PERCENT. A value of 100 is the maximum.

    Returns:
        The values that the function reads back, as percents: speed_x, speed_y,
        acceleration_x and acceleration_y. On this CD7 the two speeds are
        ``None``, because the gateway refuses GetSpeed (DEV_NOTES).
    """
    return asyncio.run(
        MS_zenapi_stage_LM.set_stage_motion(speed_percent, acceleration_percent)
    )


# ---------------------------------------------------------------------------
# The optics
# ---------------------------------------------------------------------------

def get_current_objective_and_optovar() -> Tuple[int, int]:
    """Get the current ``(objective_position, optovar_position)``."""
    return asyncio.run(MS_zenapi_objectivechanger.get_current_objective_and_optovar())


def get_current_objective_and_optovar_names() -> Tuple[Tuple[str, int], Tuple[str, int]]:
    """
    Get the current ``((objective_name, position), (optovar_name, position))``.

    An example of a name is 'Plan-Apochromat 20x/0.95' or '2x Tubelens'. Use
    the magnification and the numerical aperture to make sure that the optics
    are correct. Do not use the slot number.
    """
    return asyncio.run(
        MS_zenapi_objectivechanger.get_current_objective_and_optovar_names()
    )


def immersion_objective_active() -> bool:
    """Return ``True`` if the active objective is an immersion objective.

    The data comes from the immersion type of the API, or from the known slot.
    """
    return asyncio.run(MS_zenapi_objectivechanger.immersion_objective_active())


def _change_optics(objective_nr: int, optovar_nr: int,
                   timeout_seconds: float = 30.0,
                   poll_interval: float = OBJECTIVE_POLL_INTERVAL,
                   max_retries: int = MAX_IMMERSION_RETRIES) -> bool:
    """
    Change the objective and the optovar, then read back until the changer agrees.

    Use this function only in ``set_optics_verified``. For a change to or from
    the immersion objective, the time limit is at least
    IMMERSION_OBJECTIVE_TIMEOUT. While ZEN removes the water, it can report
    "has not been set" one time. The function then waits an additional
    IMMERSION_RETRY_DELAY.

    Returns:
        ``True`` when the read-back shows the target. ``False`` after
        ``max_retries`` time limits.

    Raises:
        ValueError: A position is not correct, or it is not in the inventory of
            the changer.
    """
    MS_limits.require_objective_number(objective_nr)
    MS_limits.require_optovar_number(optovar_nr)
    log = set_logging()

    async def _set_and_wait() -> bool:
        changer = MS_zenapi_objectivechanger
        target = (objective_nr, optovar_nr)

        # For the lens at each position, see DEV_NOTES "Optics map (this CD7)".
        start_objective, _ = await changer.get_current_objective_and_optovar()

        # This is the source that the immersion move guard uses: the type
        # from the API and the known slot.
        immersion_positions = await changer.immersion_objective_positions()
        is_from_immersion = start_objective in immersion_positions
        is_to_immersion = objective_nr in immersion_positions

        if is_from_immersion or is_to_immersion:
            adjusted_timeout = max(timeout_seconds, IMMERSION_OBJECTIVE_TIMEOUT)
            log.info(f"Immersion objective operation detected - using extended "
                     f"timeout: {adjusted_timeout}s")
        else:
            adjusted_timeout = timeout_seconds

        for attempt in range(1, max_retries + 1):
            try:
                await changer.set_objective_set_optovar(objective_nr, optovar_nr)

                start_time = time.time()
                while time.time() - start_time < adjusted_timeout:
                    if tuple(await changer.get_current_objective_and_optovar()) == target:
                        return True
                    await asyncio.sleep(poll_interval)

                log.warning(f"Objective change: timeout after {adjusted_timeout}s, "
                            f"retry {attempt}/{max_retries}")

            except (ValueError, TypeError):
                # This error does not go away. For example, the target is
                # not in the inventory. A retry cannot correct it, and a
                # return of False hides it.
                raise
            except Exception as e:
                log.warning(f"Exception during objective change: {e} — "
                            f"retry {attempt}/{max_retries}")

                # ZEN raises "has not been set" one time while it removes
                # the water after a move from the immersion objective.
                if "has not been set" in str(e) and is_from_immersion:
                    log.info("This is expected when moving from 50x immersion - hardware "
                             "needs time. Waiting for immersion removal process...")
                    await asyncio.sleep(IMMERSION_RETRY_DELAY)

                await asyncio.sleep(OBJECTIVE_CHANGE_RETRY_DELAY)

        return False

    return asyncio.run(_set_and_wait())


def set_optics_verified(objective_nr: int, optovar_nr: int,
                        settle_s: float = OPTICS_SETTLE_S,
                        timeout_s: float = OPTICS_VERIFY_TIMEOUT_S,
                        poll_s: float = OPTICS_VERIFY_POLL_S) -> None:
    """
    Change the objective and the optovar, then CHECK the read-back and WAIT.

    Use this function before each acquisition that comes after an optics
    change. ZEN can tile a scan that starts directly after a change for the old
    optics, and the mosaic is then wrong. The function reads the optics back
    until they show the target. Then it waits ``settle_s``, which is a safe
    default value and not a measured time of ZEN. Then it reads the optics back
    one more time. It stops with an error after ``timeout_s``.

    Args:
        objective_nr: The objective position, 1 to 4.
        optovar_nr: The optovar position, 1 to 3.
        settle_s: The wait after the read-back first shows the target.
        timeout_s: Stop if the read-back does not show the target and stay
            stable in this time.

    Raises:
        ValueError: A position or a time argument is not correct.
        RuntimeError: The optics do not become stable at the target.
    """
    for name, seconds in (("settle_s", settle_s), ("timeout_s", timeout_s),
                          ("poll_s", poll_s)):
        if seconds < 0:
            raise ValueError(f"{name} must be >= 0, got {seconds}")
    log = set_logging()
    target = (objective_nr, optovar_nr)

    # A return of False, which is a time limit of the changer, is not an
    # error. The read-back is the check.
    if not _change_optics(objective_nr, optovar_nr):
        log.warning(f"Optics change to {target} not confirmed by the changer — "
                    f"verifying the read-back.")

    deadline = time.monotonic() + timeout_s
    while True:
        current = tuple(get_current_objective_and_optovar())
        if current == target:
            time.sleep(settle_s)
            current = tuple(get_current_objective_and_optovar())
            if current == target:
                log.info(f"Optics confirmed + settled: objective pos {objective_nr}, "
                         f"optovar pos {optovar_nr}.")
                return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Optics never settled at objective {objective_nr}/optovar "
                               f"{optovar_nr} (read-back {current}) — aborting to avoid a "
                               f"mis-tiled scan.")
        log.info(f"Waiting for optics to reach (obj {objective_nr}, opt {optovar_nr}); "
                 f"currently {current}...")
        time.sleep(poll_s)


# ---------------------------------------------------------------------------
# The sample carrier and the tray configuration
# ---------------------------------------------------------------------------

def get_sample_carrier_info() -> Dict[str, Any]:
    """
    Get the data of the active sample carrier.

    The keys are name, rows, columns, material, thickness in µm, skirt and
    refractive_index. See ``MS_zenapi_sample_carrier.get_sample_carrier_info``.
    """
    return asyncio.run(MS_zenapi_sample_carrier.get_sample_carrier_info())


def get_sample_carrier_name() -> str:
    """Get the name of the active sample carrier."""
    return get_sample_carrier_info()["name"]


def get_current_container() -> Optional[Tuple[int, int]]:
    """
    Get the container that the stage is in, as ``(row, column)`` from 0.

    On the CD7 with the 3-slide insert, this function ALWAYS returns ``None``,
    also directly after ``move_to_container``. Thus do not use it as a check
    (DEV_NOTES "Multi-slide carriers").
    """
    return asyncio.run(MS_zenapi_sample_carrier.get_current_container())


def move_to_container(row_index: int, column_index: int,
                      focus_tracker: Optional[Resettable] = None) -> None:
    """
    Let ZEN move the stage to a container. The indices start at 0.

    For example, slide 2 of 3 is row 0, column 1. This function operates like
    ``move_stage_to_new_xy_position``. It lowers Z to 0 first, because ZEN puts
    the Z of the old slide on the new slide. Z ends near 0. The function
    refuses the move while the immersion objective is active. Focus again
    before you acquire. Do NOT use ``get_current_container`` as a check,
    because it is always ``None`` on the CD7. Use the images instead. See
    DEV_NOTES "Multi-slide carriers".

    Args:
        focus_tracker: The function resets this tracker before the move.

    Raises:
        ValueError: An index is not an integer, or it is less than 0.
        ImmersionObjectiveActive (a RuntimeError): The immersion objective
            is active. The function moves nothing.
    """
    for name, index in (("row_index", row_index), ("column_index", column_index)):
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError(f"{name} must be a non-negative integer, got {index!r}")
    # Reset before the move. This is safe, also if the move stops in the
    # middle.
    if focus_tracker is not None:
        focus_tracker.reset()
    asyncio.run(MS_zenapi_sample_carrier.move_to_container(row_index, column_index))


def get_live_scan_configuration() -> Dict[str, Any]:
    """Get the live-scan configuration of ZEN, for the tray load and the prescan.

    The keys are the names in ``MS_zenapi_live_scan.CONFIG_FIELDS``. This
    function is read-only.
    """
    return asyncio.run(MS_zenapi_live_scan.get_live_scan_configuration())


# ---------------------------------------------------------------------------
# The acquisition
# ---------------------------------------------------------------------------

def run_experiment_from_path(czexp_path: Union[str, Path],
                             custom_folder: Optional[Path] = None,
                             custom_filename: Optional[str] = None
                             ) -> MS_zenapi_experiment_methods.ExperimentResult:
    """
    Run a ``.czexp`` file, then move the ``.czi`` file to ``custom_folder``.

    The code LOADS the file by its path. It does not import the file. Thus a
    copy that you edit for one run also works.

    Args:
        custom_folder: The output folder. ``None`` gives
            DEFAULT_EXPERIMENT_OUTPUT_FOLDER.
        custom_filename: The base name of the output file, without ``.czi``.

    Returns:
        ``{exp_result_path, experiment_id}``.

    Raises:
        FileNotFoundError: ``czexp_path`` does not exist.
    """
    result = asyncio.run(MS_zenapi_experiment_methods.run_experiment_from_path(
        czexp_path=czexp_path,
        custom_image_folder=Path(custom_folder or DEFAULT_EXPERIMENT_OUTPUT_FOLDER),
        custom_filename=custom_filename,
    ))
    set_logging().info(f"Experiment results: {result}")
    return result
