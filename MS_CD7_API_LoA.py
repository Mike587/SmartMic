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
The synchronous entry point of the SmartMic library.

Use it as ``import MS_CD7_API_LoA as ms``. Each function puts the asynchronous
``MS_zenapi_*`` calls into ``asyncio.run``. It also adds the checks and the
guards that the raw calls do not have.

Rules
-----
* Each function is synchronous. It makes its own event loop and its own gRPC
  channel for each call. Do not call these functions from asynchronous code.
* Each length is in METRES. The exception is a name or a key that says
  something different, for example ``thickness_um``.
* An argument that is not correct raises ``ValueError`` or ``TypeError``
  before any hardware call. The range checks are in ``MS_limits``.
* A call that gets no answer raises ``TimeoutError``. The limit is 60 seconds
  for a read and 300 seconds for a move. An acquisition has no limit. See
  ``MS_zenapi_helpers``.
* A failure RAISES an exception (DEV_NOTES "Rule: a failure raises"). There is
  one exception to this rule. ``DefiniteFocusTracker.find_surface`` returns
  ``None`` if one position fails, and the pipeline then goes to the next
  position. It raises an exception after more failures.

The public functions
--------------------
The start of a run
    preflight                       Check the carrier, the busy state, the
                                    stage motion and the versions. It raises
                                    PreflightFailed.
    is_microscope_busy              True while an experiment or an acquisition
                                    runs.
    get_running_experiment_status   The status of that experiment, or None.
    stop_experiment                 Stop it, for a crash handler or a Ctrl-C
                                    handler.

Focus: the Z-drive, DefiniteFocus and the software autofocus
    get_current_z_position          The Z in metres.
    move_focus_to_new_z_position    An absolute Z move with a range check.
    df_find_surface                 DF FindSurface with retries. It gives the
                                    Z in metres.
    df_recall_focus                 DF RecallFocus. It gives the Z in metres.
    DefiniteFocusTracker            FindSurface with a start point that
                                    adapts, and a fault after more failures.
    swaf_find_focus                 The software autofocus from a .czexp file.
                                    It gives the Z in metres.

The stage
    get_current_xy_stage_position   [x, y] in metres.
    move_stage_to_new_xy_position   It lowers Z first. It refuses the move
                                    while the immersion objective is active.
    set_stage_motion_sync           The stage speed and acceleration, in
                                    percent.

The optics
    get_current_objective_and_optovar        The position indices.
    get_current_objective_and_optovar_names  The names and the positions.
    check_immersion_guard           Compare the immersion types of the API
                                    with the known slot.
    set_optics_verified             Change the optics, then wait for a stable
                                    read-back.

The sample carrier and the tray
    get_sample_carrier_info         All the data of the carrier.
    get_sample_carrier_name         The name of the carrier.
    carrier_bottom                  The name, the material and the thickness
                                    of the bottom.
    check_carrier_for_df            Compare the bottom that DF uses with the
                                    value from the API.
    wait_for_df_bottom              Wait until DF has the bottom after a load.
    get_current_container           The slide position in an insert.
    move_to_container               Move to a slide position in an insert.
    get_live_scan_configuration     What the prescan does at a tray load.
    eject_tray                      A tray move with guards.
    load_tray_and_prescan           A tray move with guards.

The acquisition
    run_experiment_from_path        Run a ``.czexp`` file.

The checks. Each one returns ``(ok, message)``, and the moves call them.
    validate_objective_number, validate_optovar_number, validate_z_position,
    validate_xy_position

The exceptions
    FocusSearchFailed               One DF search or SWAF search failed.
    PreflightFailed                 A preflight check failed.
    DefiniteFocusFault              DF failed more than one time. Stop the run.
    CarrierChanged                  This is a DefiniteFocusFault.
    MS_zenapi_stage_LM.ImmersionObjectiveActive
                                    The code refused an XY move.

For the behaviour of the hardware, and for the reasons for the guards, see
DEV_NOTES.md.
"""

import asyncio
import logging
import math
import time
from pathlib import Path
from typing import (Any, Callable, Dict, List, NoReturn, NotRequired, Optional, Tuple,
                    TypedDict, Union)

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


# The configuration constants.
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

# The default values of DefiniteFocusTracker.
DF_APPROACH_MARGIN_M = 100e-6       # A search starts this far below the last surface.
DF_MAX_CONSECUTIVE_FAILURES = 5     # This many failures in sequence is a hardware fault.

# These names come from MS_limits. Change a limit in that module.
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
    """The bottom of the active sample carrier. See ``carrier_bottom``."""
    name: str
    material: str
    thickness_um: float
    refractive_index: NotRequired[float]   # The DF log event does not hold this.


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


def get_current_z_position() -> float:
    """Get the current Z-drive position in metres."""
    return asyncio.run(MS_zenapi_focus.get_current_z_focus_position())


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


class PreflightFailed(RuntimeError):
    """``preflight`` found a reason not to start the run. See the message."""


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


# Two thicknesses in µm that are nearer to each other than this value are
# equal. The API and the DF log round the value in different ways.
CARRIER_THICKNESS_TOL_UM = 0.5


def carrier_bottom() -> CarrierBottom:
    """Get the bottom of the active sample carrier from the API.

    The call needs approximately 10 ms. ``thickness_um`` is the measured
    value, if ZEN measured it at the load.
    """
    info = get_sample_carrier_info()
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
            MS_zenapi_stage_LM.ImmersionObjectiveActive: The tracker knows no
                surface and the immersion objective is active.
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
        """Count one failed search, and raise DefiniteFocusFault at the limit."""
        self.consecutive_failures += 1
        self.last_message = str(e)
        set_logging().warning(f"DF failed ({self.consecutive_failures} in a row): {e}")
        if self.consecutive_failures >= self.max_consecutive_failures:
            raise DefiniteFocusFault(
                f"DefiniteFocus failed at {self.consecutive_failures} positions in a "
                f"row (last: {e}) — treating it as a hardware fault.")
        return None


def _immersion_objective_active() -> bool:
    """Return ``True`` if the active objective is an immersion objective.

    The data comes from the immersion type of the API, or from the known slot.
    """
    return asyncio.run(MS_zenapi_stage_LM.immersion_objective_active())


def check_immersion_guard(log: logging.Logger) -> Optional[bool]:
    """Compare the immersion objectives of the API with the known slot.

    The guard refuses a move of Z to 0 for each source: the immersion type of
    the API, and MS_zenapi_stage_LM.IMMERSION_OBJECTIVE_POSITION. A difference
    means that someone changed the turret or the constant. The guard then still
    refuses the move for both sources, but the immersion time limits and the
    tray code can be wrong. This function is read-only. Preflight writes the
    result to the log and does not stop the run.

    Returns:
        ``True`` if the two sources agree. ``False`` if they do not agree, and
        the function writes a ``log.error``. ``None`` if the function cannot
        read the inventory, and it writes a ``log.error``.
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


def _validate_tolerance(tolerance_m: Optional[float]) -> None:
    if tolerance_m is None:
        return
    # Refuse a value that is not finite. A value of inf accepts a failed move
    # at any position.
    if (isinstance(tolerance_m, bool) or not isinstance(tolerance_m, (int, float))
            or not math.isfinite(tolerance_m) or tolerance_m < 0):
        raise ValueError(f"tolerance_m must be a finite, non-negative number (metres) "
                         f"or None, got {tolerance_m!r}")


def move_focus_to_new_z_position(new_z_pos: float,
                                 tolerance_m: Optional[float] = None) -> None:
    """
    Move the Z-drive to an absolute position in metres.

    WARNING: the function checks only the range, with ``validate_z_position``.
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
    _require_valid(validate_z_position(new_z_pos), "Z position")
    _validate_tolerance(tolerance_m)

    set_logging().info(f"Moving focus to Z = {new_z_pos * 1e6:.3f} µm")
    _move_with_tolerance(
        lambda: asyncio.run(MS_zenapi_focus.move_focus_to_new_z_position(new_z_pos)),
        lambda: abs(asyncio.run(MS_zenapi_focus.get_current_z_focus_position()) - new_z_pos),
        tolerance_m, f"Z move to {new_z_pos * 1e6:.3f} µm")


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
        MS_zenapi_stage_LM.ImmersionObjectiveActive (a RuntimeError): The 50x
            immersion objective is active. A Z of 0 breaks the water bridge.
            Thus the function refuses the move, and it never tries the move
            again. Write the target position into a ``.czexp`` file and let ZEN
            run it (DEV_NOTES).
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
    _require_valid(validate_objective_number(objective_nr), "objective number")
    _require_valid(validate_optovar_number(optovar_nr), "optovar number")
    log = set_logging()

    async def _set_and_wait() -> bool:
        # For the lens at each position, see DEV_NOTES "Optics map (this CD7)".
        current_obj, current_opt = (
            await MS_zenapi_objectivechanger.get_current_objective_and_optovar())

        # This is the source that the immersion move guard uses: the type
        # from the API and the known slot.
        immersion_positions = await MS_zenapi_stage_LM.immersion_objective_positions()
        is_from_immersion = current_obj in immersion_positions
        is_to_immersion = objective_nr in immersion_positions

        if is_from_immersion or is_to_immersion:
            adjusted_timeout = max(timeout_seconds, IMMERSION_OBJECTIVE_TIMEOUT)
            log.info(f"Immersion objective operation detected - using extended "
                     f"timeout: {adjusted_timeout}s")
        else:
            adjusted_timeout = timeout_seconds

        retry_count = 0
        while retry_count < max_retries:
            try:
                await MS_zenapi_objectivechanger.set_objective_set_optovar(objective_nr, optovar_nr)

                start_time = time.time()

                while time.time() - start_time < adjusted_timeout:
                    current_obj, current_opt = (
                        await MS_zenapi_objectivechanger.get_current_objective_and_optovar())

                    if current_obj == objective_nr and current_opt == optovar_nr:
                        return True

                    await asyncio.sleep(poll_interval)

                log.warning(f"Objective change: timeout after {adjusted_timeout}s, "
                            f"retry {retry_count + 1}/{max_retries}")
                retry_count += 1

            except (ValueError, TypeError):
                # This error does not go away. For example, the target is
                # not in the inventory. A retry cannot correct it, and a
                # return of False hides it.
                raise
            except Exception as e:
                log.warning(f"Exception during objective change: {e} — "
                            f"retry {retry_count + 1}/{max_retries}")

                # ZEN raises "has not been set" one time while it removes
                # the water after a move from the immersion objective.
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
    for name, v in (("settle_s", settle_s), ("timeout_s", timeout_s), ("poll_s", poll_s)):
        if v < 0:
            raise ValueError(f"{name} must be >= 0, got {v}")
    log = set_logging()
    target = (objective_nr, optovar_nr)
    # A return of False, which is a time limit of the changer, is not an
    # error. The read-back is the check.
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


def get_live_scan_configuration() -> Dict[str, Any]:
    """Get the live-scan configuration of ZEN, for the tray load and the prescan.

    The keys are the names in ``MS_zenapi_live_scan.CONFIG_FIELDS``. This
    function is read-only.
    """
    return asyncio.run(MS_zenapi_live_scan.get_live_scan_configuration())


# The dry 5x/0.35 objective. Change to it before an eject from the 50x
# immersion objective.
EJECT_DRY_OBJECTIVE = 2


def eject_tray(switch_to_dry_objective: bool = True,
               focus_tracker: Optional[DefiniteFocusTracker] = None) -> List[str]:
    """
    Move the sample-carrier tray out with ``LiveScanService.EjectTray``.

    ZEN stops DF and moves Z back itself. Thus this function makes no Z move
    (DEV_NOTES "Tray load / eject").

    Args:
        switch_to_dry_objective: If the 50x immersion objective is active,
            change first to EJECT_DRY_OBJECTIVE, which is the 5x, with the
            current optovar and ``set_optics_verified``. This is the safe
            procedure. With ``False``, the function ejects the tray with the
            50x objective in place.
        focus_tracker: The function resets this tracker before the eject.

    Returns:
        The notifications from ZEN.

    Raises:
        RuntimeError: An experiment runs, and the function moves nothing. Or
            the change to the dry objective does not become stable, and the
            function does not eject the tray.
    """
    log = set_logging()
    if is_microscope_busy():
        raise RuntimeError("Microscope is busy (an experiment runs) — tray not ejected.")
    if focus_tracker is not None:
        focus_tracker.reset()
    if switch_to_dry_objective and _immersion_objective_active():
        _, optovar = get_current_objective_and_optovar()
        log.info(f"Eject: immersion objective active — changing to objective "
                 f"{EJECT_DRY_OBJECTIVE} (5x) first.")
        # This raises RuntimeError if the optics do not settle. Then no eject occurs.
        set_optics_verified(EJECT_DRY_OBJECTIVE, optovar)
    return asyncio.run(MS_zenapi_live_scan.eject_tray())


# After a tray load, wait until DF has a stable bottom thickness that is more
# than 0.
LOAD_SETTLE_TIMEOUT_S = 60.0
LOAD_SETTLE_POLL_S = 1.0


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
    Move the tray in and run the prescan with ``LoadTrayAndPrescan``.

    For the prescan steps, see ``get_live_scan_configuration``. After the load,
    ZEN leaves the 5x objective, which is position 2, in place. Thus set the
    objective again before you acquire. Do not start DF while a load runs.
    After the load, this function waits with ``wait_for_df_bottom``.

    Args:
        focus_tracker: The function resets this tracker before the load.

    Returns:
        The notifications from ZEN.

    Raises:
        RuntimeError: An experiment runs, and the function moves nothing. Or DF
            has no carrier thickness 60 seconds after the load. See
            ``wait_for_df_bottom``.
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
        MS_zenapi_stage_LM.ImmersionObjectiveActive (a RuntimeError): The
            immersion objective is active. The function moves nothing.
    """
    for name, v in (("row_index", row_index), ("column_index", column_index)):
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError(f"{name} must be a non-negative integer, got {v!r}")
    # Reset before the move. This is safe, also if the move stops in the
    # middle.
    if focus_tracker is not None:
        focus_tracker.reset()
    asyncio.run(MS_zenapi_sample_carrier.move_to_container(row_index, column_index))


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


def is_microscope_busy() -> bool:
    """Return ``True`` while an experiment or an acquisition runs.

    See ``get_running_experiment_status``.
    """
    status = get_running_experiment_status()
    if status is None:
        return False
    return bool(status["is_experiment_running"] or status["is_acquisition_running"])


def _fail(log: logging.Logger, msg: str) -> NoReturn:
    """Write a preflight failure to the log, and raise PreflightFailed with that text."""
    log.error(msg)
    raise PreflightFailed(f"Preflight failed: {msg}")


def preflight(expected_carrier: str, log: logging.Logger,
              stage_speed_percent: Optional[float] = None,
              stage_acceleration_percent: Optional[float] = None,
              expected_material: Optional[str] = None,
              thickness_range_um: Optional[Tuple[float, float]] = None) -> None:
    """
    The usual preflight at the start of a run. Run it before ANY hardware action.

    The function does these checks, in this order. The gateway must answer, and
    the carrier request is the test. The microscope must be idle. ZEN must have
    ``expected_carrier``. The DF carrier bottom must be correct, through
    ``check_carrier_for_df`` with ``expected_material`` and
    ``thickness_range_um`` in µm. Then the function sets the stage speed and the
    acceleration one time for the run, with ``set_stage_motion_sync``, and
    writes the read-back to the log.

    The function only writes to the log, and it never stops the run, for these
    items: the versions, the auto-immersion refill settings, the live-scan
    configuration and ``check_immersion_guard``.

    Args:
        log: The logger for all the messages. They go into the log of this run.
        stage_speed_percent, stage_acceleration_percent: A percent of the
            maximum. ``None`` gives the default values of MS_zenapi_stage_LM,
            and 100 is the maximum. Use a lower value for a live sample. If you
            give one of these values and the device does not accept it,
            preflight fails. If both are ``None``, a setting that the device
            refuses only gives a warning.

    Raises:
        PreflightFailed: A check failed. The function also writes the reason
            with ``log.error``. Nothing moved.
    """
    # 0. The versions. The gRPC API has no version service, thus the code
    #    reads them from other sources.
    sm_ver = zeiss_paths.smartmic_version()
    zen_ver = MS_zen_logs.zen_app_version()
    gw_ver = zeiss_paths.zen_api_gateway_version()
    api_ver, api_path = zeiss_paths.zen_api_version()
    log.info(f"SmartMic: {sm_ver or 'unknown (not a git checkout?)'}  |  "
             f"ZEN app: {zen_ver or 'unknown'}  |  "
             f"ZenApiGateway: {gw_ver or 'unknown'}  |  "
             f"zen_api stubs: {api_ver or 'unversioned (loose layout)'} @ {api_path}")

    # 0b. The auto-immersion refill settings. Log only. The API does not give
    #     them (DEV_NOTES "The re-immersion settings are NOT reachable through
    #     the ZEN-API").
    try:
        log.info(MS_zen_logs.format_auto_immersion_settings(
            MS_zen_logs.zen_auto_immersion_settings()))
    except Exception as e:
        log.info(f"Auto-immersion refill settings: could not read ({e})")

    # 1. Test that the gateway answers.
    try:
        carrier = get_sample_carrier_name()
    except Exception as e:
        _fail(log, f"ZEN-API gateway not responding ({e}) — is ZEN running? Aborting.")

    # 1b. The live-scan configuration. Log only. It shows a difference
    #     between the template and the carrier (DEV_NOTES "Tray load /
    #     eject").
    try:
        log.info(MS_zenapi_live_scan.format_live_scan_configuration(
            get_live_scan_configuration(), carrier=carrier))
    except Exception as e:
        log.info(f"Live-scan config: could not read ({e})")

    # 1c. Do the two sources of the immersion guard agree? Log only. The
    #     guard refuses a move for both sources.
    check_immersion_guard(log)

    # 2. The microscope must be idle.
    try:
        busy = is_microscope_busy()
    except Exception as e:
        _fail(log, f"Could not query microscope status ({e}) — aborting.")
    if busy:
        _fail(log, "Microscope is busy (an experiment is already running) — aborting.")

    # 3. The carrier must be the correct one.
    if carrier != expected_carrier:
        _fail(log, f"Wrong sample carrier: '{carrier}' "
                  f"(expected '{expected_carrier}') — aborting.")

    # 3b. The DF carrier bottom. A wrong value moves the DF focus on a dry
    #     objective, and it gives no message.
    if check_carrier_for_df(log, expected_material, thickness_range_um) is None:
        _fail(log, "DF carrier check failed — aborting.")

    # 4. The stage speed and the acceleration. If you ask for a lower value
    #    and the device does not accept it, the run stops, because the run then
    #    goes at the full speed and gives no message. If you ask for no value,
    #    a refusal only gives a warning. set_stage_motion already accepts a
    #    refusal of SetSpeed from the gateway (DEV_NOTES). Thus an exception
    #    here means that the device did not accept the ACCELERATION.
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
    log.info(f"Preflight OK: gateway responsive, microscope idle, "
             f"carrier '{carrier}', {motion_str}.")
