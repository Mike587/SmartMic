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

A pipeline imports this module (``import MS_CD7_API_LoA as ms``) and calls plain
functions. Each function wraps one or more async ZEN gRPC calls of the
``MS_zenapi_*`` modules in ``asyncio.run``, and adds the checks and guards that
the raw calls do not have.

Rules
-----
* Synchronous only. Each call makes its own event loop and its own gRPC
  channel: do not call these functions from async code.
* Lengths are METRES. The exceptions have the unit in a name or a key:
  carrier thickness is µm (``thickness_um``). The legacy functions ``run_swaf``
  / ``run_swaf_from_path`` / ``run_definite_focus_recall`` return µm; use
  ``swaf_find_focus`` / ``df_recall_focus`` (metres) in new code.
* A bad argument raises ``ValueError`` / ``TypeError`` before any hardware call.
  The range checks are in ``MS_limits``; the async moves and the ``.czexp``
  writers use the same ones.
* A call that gets no answer raises ``TimeoutError`` after 60 s (reads) or
  300 s (moves); an acquisition has no limit (``MS_zenapi_helpers``).
* A failure RAISES (DEV_NOTES "Rule: a failure raises"). Use these functions
  in new code:

      df_find_surface, df_recall_focus       -> FocusSearchFailed
      swaf_find_focus, swaf_find_focus_from_path -> FocusSearchFailed
      set_optics_verified                    -> RuntimeError
      require_preflight                      -> PreflightFailed

  The legacy functions report a failure in the return value, and a caller that
  does not examine it continues with no error:
  ``run_definite_focus_find_surface`` (``(False, message, attempts)``),
  ``run_definite_focus_recall`` / ``run_swaf`` (``(None, attempts)``),
  ``set_objective_set_optovar_sync`` / ``preflight`` (``False``). They stay for
  the existing projects and are not changed.
  ``DefiniteFocusTracker.find_surface`` returns None for one failed position
  on purpose (the pipeline skips it) and raises after repeated failures.

Public functions
----------------
Start of a run
    require_preflight               -- carrier, busy state, stage motion, versions;
                                       raises PreflightFailed
    preflight                       -- legacy: the same, returns False on failure
    is_microscope_busy              -- True while an experiment or acquisition runs
    get_running_experiment_status   -- status of that experiment, or None
    stop_experiment                 -- stop it (for a crash / Ctrl-C handler)

Focus (Z-drive, DefiniteFocus, software autofocus)
    get_current_z_position          -- Z in metres
    move_focus_to_new_z_position    -- absolute Z move, range-checked
    df_find_surface                 -- DF FindSurface with retries -> Z (m)
    df_recall_focus                 -- DF RecallFocus -> Z (m)
    swaf_find_focus / swaf_find_focus_from_path
                                    -- software autofocus by name / path -> Z (m)
    DefiniteFocusTracker            -- FindSurface with an adaptive start and a
                                       fault after repeated failures
    run_definite_focus_find_surface, run_definite_focus_recall, run_swaf,
    run_swaf_from_path              -- legacy: failure in the return value, µm

Stage
    get_current_xy_stage_position   -- [x, y] in metres
    move_stage_to_new_xy_position   -- lowers Z first; refuses with the immersion
                                       objective active
    set_stage_motion_sync           -- stage speed / acceleration in percent

Optics
    get_current_objective_and_optovar / _names -- position indices / names
    check_immersion_guard           -- API immersion types against the known slot
    set_optics_verified             -- change, then require a stable read-back;
                                       raises if it does not settle
    set_objective_set_optovar_sync  -- legacy: returns False on a timeout

Sample carrier and tray
    get_sample_carrier_info / get_sample_carrier_name
    carrier_bottom                  -- name, material, thickness of the bottom
    check_carrier_for_df            -- the bottom DF uses, against the API value
    wait_for_df_bottom              -- wait until DF has the bottom after a load
    get_current_container / move_to_container  -- slide positions of an insert
    get_live_scan_configuration     -- what the prescan does on a tray load
    eject_tray / load_tray_and_prescan         -- guarded tray moves

Acquisition
    run_experiment                  -- by name, from ZEN's experiment library
    run_experiment_from_path        -- a ``.czexp`` by path
    run_experiment_from_xml         -- an experiment XML string

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

# Hardware limits and the checks that use them are in MS_limits (one module for
# all layers). The names below are copies for callers of this module: to change
# a limit, set it in MS_limits.
from MS_limits import (  # noqa: E402,F401
    OBJECTIVE_MIN, OBJECTIVE_MAX, OPTOVAR_MIN, OPTOVAR_MAX,
    Z_POSITION_MIN, Z_POSITION_MAX,
    X_STAGE_MIN, X_STAGE_MAX, Y_STAGE_MIN, Y_STAGE_MAX,
    validate_objective_number, validate_optovar_number,
    validate_z_position, validate_xy_position,
)
from MS_limits import validate_range as _validate_range  # noqa: E402,F401
from MS_limits import require_valid as _require_valid    # noqa: E402

# Default experiment output folder — single-sourced from MS_run_log
# so the literal path lives in exactly one place.
DEFAULT_EXPERIMENT_OUTPUT_FOLDER = MS_run_log.DEFAULT_EXPERIMENT_OUTPUT_FOLDER


class CarrierBottom(TypedDict):
    """The bottom of the active sample carrier (``carrier_bottom``)."""
    name: str
    material: str
    thickness_um: float
    refractive_index: NotRequired[float]   # not in the DF log event


def run_definite_focus_find_surface(max_retries: int = 3,
                                    start_z_m: Optional[float] = None,
                                    store_focus: bool = False) -> Tuple[bool, str, int]:
    """
    Run definite focus find surface with retry logic.

    Legacy: a failed search is in the return value, not an exception. New code:
    df_find_surface (raises FocusSearchFailed, returns the surface Z in metres).

    Args:
        max_retries:          Maximum number of retry attempts (default: 3).
        start_z_m:            Z position in metres to move to before searching.
                                    None → use the async function's default (−300 µm).
                                    Pass last_surface_z − 100e-6 to speed up subsequent DFs.
        store_focus:         Do StoreFocus after a successful search (default
                                    False). Set it only if you use
                                    run_definite_focus_recall later.

    Returns:
        (success, message, attempts_used)

    Raises:
        ValueError, TypeError: For a bad argument (e.g. max_retries < 1). Only
            a failed search gives (False, message, attempts).
    """
    kwargs = {"max_retries": max_retries}
    if start_z_m is not None:
        kwargs["start_z_m"] = start_z_m
    if store_focus:
        kwargs["store_focus"] = True
    try:
        # definite_focus_find_surface only ever returns on success — it raises
        # (with attempts_used attached) after exhausting max_retries — so there
        # is no separate "clean" False outcome to branch on here.
        success, attempts = asyncio.run(
            MS_zenapi_focus.definite_focus_find_surface(**kwargs)
        )
        return True, "Definite focus successful", attempts
    except (ValueError, TypeError):
        # A bad argument is a programming error, not a failed search: the
        # tracker must not count it as a DF failure and carry on.
        raise
    except Exception as e:
        attempts_used = getattr(e, "attempts_used", max_retries)
        return False, f"Definite focus failed with exception: {e}", attempts_used


def run_definite_focus_recall(max_retries: int = 3) -> Tuple[Optional[float], int]:
    """
    Run DefiniteFocus RecallFocus: go back to the stored distance from the reflex.

    This is a fast operation (0.7 to 2 s) that requires a previous FindSurface +
    StoreFocus to have been executed first:
    run_definite_focus_find_surface(store_focus=True).

    Measured on the CD7 on 2026-11-09 (DEV_NOTES "What each DF RPC does"):
    - 20x and 50x objectives: Recall goes to the reflex plus the stored offset
      (±0.15 µm), also after a new FindSurface.
    - 5x objective: Recall goes to the reflex. It does not use the stored offset,
      because StoreFocus on the 5x records no offset.
    - After an objective change: Recall does a full FindSurface. The stored offset
      is not used. On the 50x objective this is a search without a seed (see
      DEV_NOTES "50x: DF locks onto the plate UNDERSIDE").

    Legacy: the Z is in MICROMETRES and a failure is ``(None, attempts)``. New
    code: df_recall_focus (metres, raises FocusSearchFailed).

    Returns:
        Tuple (z_um, attempts_used).  z_um is None if all attempts fail.
    """
    return asyncio.run(MS_zenapi_focus.definite_focus_recall(max_retries))


def df_find_surface(max_retries: int = 3, start_z_m: Optional[float] = None,
                    store_focus: bool = False) -> float:
    """
    Run DefiniteFocus FindSurface and return the surface Z in metres.

    The same search as run_definite_focus_find_surface (same arguments, same
    retries). The difference: a failed search raises.

    Args:
        max_retries: Maximum number of attempts.
        start_z_m: Z in metres to start the search from. None: the default of
            the async function (-300 µm).
        store_focus: Do StoreFocus after a successful search (for
            df_recall_focus later).

    Returns:
        The Z-drive position after the search, in metres.

    Raises:
        FocusSearchFailed: No surface found in max_retries attempts.
        ValueError, TypeError: For a bad argument.
    """
    ok, message, attempts = run_definite_focus_find_surface(max_retries, start_z_m, store_focus)
    if not ok:
        raise FocusSearchFailed("FindSurface", message, attempts)
    return get_current_z_position()


def df_recall_focus(max_retries: int = 3) -> float:
    """
    Run DefiniteFocus RecallFocus and return the Z in metres.

    The same operation as run_definite_focus_recall (see there for what Recall
    does on each objective). The differences: metres, and a failure raises.

    Returns:
        The Z-drive position after the recall, in metres.

    Raises:
        FocusSearchFailed: All attempts failed.
    """
    z_um, attempts = run_definite_focus_recall(max_retries)
    if z_um is None:
        raise FocusSearchFailed("RecallFocus", "no focus position returned", attempts)
    return z_um * 1e-6


def get_current_z_position() -> float:
    """
    Get current z-drive position.

    Returns:
        Current Z position in meters
    """
    return asyncio.run(MS_zenapi_focus.get_current_z_focus_position())


class FocusSearchFailed(RuntimeError):
    """One focus search (DF FindSurface, DF RecallFocus or SWAF) found no focus.

    Raised by df_find_surface, df_recall_focus, swaf_find_focus and
    swaf_find_focus_from_path. A caller can skip the position or stop.

    Attributes:
        method: "FindSurface", "RecallFocus" or "SWAF".
        attempts: Number of attempts that were made.
    """

    def __init__(self, method: str, message: str, attempts: int) -> None:
        super().__init__(f"{method} failed after {attempts} attempt(s): {message}")
        self.method = method
        self.attempts = attempts


class PreflightFailed(RuntimeError):
    """require_preflight found a reason not to start the run (see the message)."""


class DefiniteFocusFault(RuntimeError):
    """DefiniteFocus failed at too many positions in a row: treat as a hardware fault."""


class CarrierChanged(DefiniteFocusFault):
    """The carrier bottom that DF uses changed during the run, or is 0 (a load runs).

    A subclass of DefiniteFocusFault, so a pipeline that stops on a DF fault
    also stops here: a changed thickness or material moves the DF focus by
    tens of µm on the dry objectives, and DF still reports quality 99
    (DEV_NOTES "Dry objectives: DF finds the plate UNDERSIDE").
    """


# Two thickness values that differ by less than this are the same (µm). ZEN
# reports e.g. 176.17894736842106 over the API and 176179 nm in the DF log.
CARRIER_THICKNESS_TOL_UM = 0.5


def carrier_bottom() -> CarrierBottom:
    """The bottom of the active sample carrier, over the API (approximately 10 ms).

    Returns:
        Dict with name, material, thickness_um and refractive_index. When the
        thickness was measured at load, the value is the measured one.
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
    """A zen_df_bottom() dict in the shape of carrier_bottom(), for _carrier_difference.

    The DF log event has no carrier name, so the name of the API carrier is used.
    """
    return {"name": name, "material": df["material"], "thickness_um": df["thickness_um"]}


def check_carrier_for_df(log: logging.Logger, expected_material: Optional[str] = None,
                         thickness_range_um: Optional[Tuple[float, float]] = None
                         ) -> Optional[CarrierBottom]:
    """Check the carrier bottom that DF uses. Use it at run start (preflight).

    Checks:
      1. The thickness over the API is more than 0 (0: a tray load runs).
      2. The material is ``expected_material`` (if given).
      3. The thickness is in ``thickness_range_um`` (if given).
      4. The value DF last received (ZEN log) is not 0 and agrees with the API.
         If the event is not in the logs (rotated), this is logged only.
    Logs a warning, not an error, if "measure bottom thickness" is off in the
    live-scan configuration: then the thickness is the template value.

    Args:
        log: A logger (anything with .info/.warning/.error).
        expected_material: E.g. "Glass" or "Polystyrene" (case is ignored).
        thickness_range_um: (min, max) in µm, e.g. (150, 190) for #1.5 glass.

    Returns:
        The carrier_bottom() dict if all checks pass, else None (the reason is
        logged with log.error).
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

    Carrier check (``carrier_check``, default True): before each search the
    tracker reads the carrier bottom over the API (carrier_bottom,
    approximately 10 ms). The first call records it. If the material or the
    thickness changes later, or the thickness is 0 (a tray load runs), it
    raises CarrierChanged (a DefiniteFocusFault): the run must stop. reset()
    forgets the record, so a new plate after a load is accepted. The full
    check (ZEN log, expected material and range) is check_carrier_for_df in
    preflight.

    Args:
        approach_margin_m: adaptive start distance below the last surface (m).
        max_consecutive_failures: failed searches in a row before the stop.
        max_retries: FindSurface attempts per search (run_definite_focus_find_surface).
        surface_z_m: known surface to start from (e.g. from an earlier run), or None.
        carrier_check: compare the carrier bottom before each search (default True).
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
        self.last_message = ""
        self.last_attempts = 0
        self.carrier_check = carrier_check
        self.carrier: Optional[CarrierBottom] = None

    def reset(self) -> None:
        """Forget the last surface and the recorded carrier (the failure count
        is kept: a slide change does not fix a DefiniteFocus fault)."""
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
            The surface Z in metres (drive read-back), or None if the search
            failed (the position should be skipped).

        Raises:
            DefiniteFocusFault: on the max_consecutive_failures-th failure in a row.
            CarrierChanged: (a DefiniteFocusFault) the carrier bottom changed or is
                0 (see the class docstring). Nothing is moved.
            MS_zenapi_stage_LM.ImmersionObjectiveActive: if no surface is known
                and the immersion objective is active (see the class docstring).
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
    """True if the active objective is an immersion objective.

    From the API immersion type, or the known slot
    (MS_zenapi_stage_LM.immersion_objective_active).
    """
    return asyncio.run(MS_zenapi_stage_LM.immersion_objective_active())


def check_immersion_guard(log: logging.Logger) -> Optional[bool]:
    """Compare the immersion objectives the API reports with the known slot.

    The immersion guard refuses a Z=0 lower if the API says the active
    objective is an immersion objective, or if it is in slot
    MS_zenapi_stage_LM.IMMERSION_OBJECTIVE_POSITION. The two must say the same.
    A difference means the turret changed or the constant is wrong: the guard
    still refuses for both, but the extended timeouts and the tray logic may
    not match the hardware. Read-only; preflight calls it and does not abort.

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
        new_z_pos: New Z position in meters
        tolerance_m: Tolerate ZEN's spurious "Requested
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
    _require_valid(validate_z_position(new_z_pos), "Z position")
    _validate_tolerance(tolerance_m)

    set_logging().info(f"Moving focus to Z = {new_z_pos * 1e6:.3f} µm")
    _move_with_tolerance(
        lambda: asyncio.run(MS_zenapi_focus.move_focus_to_new_z_position(new_z_pos)),
        lambda: abs(asyncio.run(MS_zenapi_focus.get_current_z_focus_position()) - new_z_pos),
        tolerance_m, f"Z move to {new_z_pos * 1e6:.3f} µm")


def get_current_xy_stage_position() -> List[float]:
    """
    Get current stage XY coordinates.

    Returns:
        [x, y] coordinates in meters
    """
    return asyncio.run(MS_zenapi_stage_LM.get_current_xy_stage_coordinates())


def move_stage_to_new_xy_position(new_x: float, new_y: float,
                                  tolerance_m: Optional[float] = None) -> None:
    """
    Move stage to absolute coordinates (in meters).

    This lowers the objective to Z = 0 before moving the stage (collision
    safety) and leaves it there — it does NOT restore the prior Z. Callers
    must re-establish focus at the new position before acquiring.

    Args:
        new_x: X coordinate in meters
        new_y: Y coordinate in meters
        tolerance_m: Tolerate ZEN's spurious "Position could
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

    Speed/acceleration are device state, not per-move parameters: the controller
    holds them across SmartMic's per-call channel open/close, so this is the
    once-per-run "global stage-speed" knob (set it in preflight and leave it).
    Both are set together; for a live sample it is the acceleration (jerk) that
    disturbs the medium, so a low speed with a high acceleration would still jolt
    it. None falls back to the module defaults in MS_zenapi_stage_LM
    (STAGE_TRAVEL_SPEED_PERCENT / STAGE_ACCELERATION_PERCENT, both 100 = full).

    Args:
        speed_percent: XY travel speed, percent of max.
        acceleration_percent: XY acceleration, percent of max.

    Returns:
        Read-back after setting, with keys speed_x, speed_y, acceleration_x,
        acceleration_y (percent). The speed values are None on this CD7: the
        gateway rejects GetSpeed (DEV_NOTES).
    """
    return asyncio.run(
        MS_zenapi_stage_LM.set_stage_motion(speed_percent, acceleration_percent)
    )


def run_experiment(experiment_name: str,
                   custom_folder: Optional[Path] = None,
                   custom_filename: Optional[str] = None) -> Dict[str, Any]:
    """
    Run an experiment by name (from ZEN's experiment library).

    Load the named experiment, run it, collect the result. This is the
    acquisition path. The smoke test of the whole experiment API is
    ``MS_zenapi_experiment_smoke.check_experiment_api``.

    Args:
        experiment_name: Name of the experiment to run
        custom_folder: Custom output folder. Uses DEFAULT_EXPERIMENT_OUTPUT_FOLDER if None.
        custom_filename: Custom filename for output

    Returns:
        Experiment results ({exp_result_path, snap_path,
        experiment_id}).

    Raises:
        ValueError: If experiment_name is empty or invalid
    """
    if not experiment_name or not isinstance(experiment_name, str):
        raise ValueError(f"Invalid experiment name: {experiment_name}")

    result = asyncio.run(MS_zenapi_experiment_methods.run_experiment_by_name(
        experiment_name=experiment_name,
        custom_image_folder=Path(custom_folder or DEFAULT_EXPERIMENT_OUTPUT_FOLDER),
        custom_filename=custom_filename,
    ))
    set_logging().info(f"Experiment results: {result}")
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
        czexp_path:           Path to the .czexp file to run.
        custom_folder:  Output folder. Uses
                                         DEFAULT_EXPERIMENT_OUTPUT_FOLDER if None.
        custom_filename: Output base filename (without .czi).

    Returns:
        {exp_result_path, snap_path, experiment_id}

    Raises:
        FileNotFoundError: If czexp_path does not exist.
    """
    czexp_path = Path(czexp_path)
    if not czexp_path.exists():
        raise FileNotFoundError(f"Experiment file not found: {czexp_path}")

    result = asyncio.run(MS_zenapi_experiment_methods.run_experiment_from_path(
        czexp_path=czexp_path,
        custom_image_folder=Path(custom_folder or DEFAULT_EXPERIMENT_OUTPUT_FOLDER),
        custom_filename=custom_filename,
    ))
    set_logging().info(f"Experiment results: {result}")
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
        xml:                       The experiment XML string.
        custom_folder:  Output folder. Uses
                                         DEFAULT_EXPERIMENT_OUTPUT_FOLDER if None.
        custom_filename: Output base filename (without .czi).

    Returns:
        {exp_result_path, snap_path, experiment_id}
    """
    result = asyncio.run(MS_zenapi_experiment_methods.run_experiment_from_xml(
        xml=xml,
        custom_image_folder=Path(custom_folder or DEFAULT_EXPERIMENT_OUTPUT_FOLDER),
        custom_filename=custom_filename,
    ))
    set_logging().info(f"Experiment results: {result}")
    return result


def run_swaf(swaf_experiment_name: str, timeout: int = 30) -> Tuple[Optional[float], int]:
    """
    Run software autofocus (SWAF) using the named experiment.

    Args:
        swaf_experiment_name: Name of the SWAF experiment (without .czexp extension).
        timeout:              Search timeout in seconds (default 30).

    Legacy: the focus position is in MICROMETRES and a failure is
    ``(None, attempts)``. New code: swaf_find_focus (metres, raises
    FocusSearchFailed).

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

    Legacy: the focus position is in MICROMETRES and a failure is
    ``(None, attempts)``. New code: swaf_find_focus_from_path (metres, raises
    FocusSearchFailed).

    Returns:
        Tuple (focus_pos_um, attempts_used).
        focus_pos_um is None if all attempts failed.
    """
    return asyncio.run(MS_zenapi_swaf.run_software_autofocus_from_path(czexp_path, timeout=timeout))


def swaf_find_focus(swaf_experiment_name: str, timeout: int = 30) -> float:
    """
    Run software autofocus with a named experiment; return the focus Z in metres.

    The same search as run_swaf. The differences: metres, and a failure raises.

    Args:
        swaf_experiment_name: Name of the SWAF experiment (without .czexp).
        timeout: Search timeout in seconds, for ZEN.

    Returns:
        The focus position that ZEN found, in metres.

    Raises:
        FocusSearchFailed: All attempts failed.
    """
    focus_um, attempts = run_swaf(swaf_experiment_name, timeout=timeout)
    if focus_um is None:
        raise FocusSearchFailed("SWAF", f"no focus found with '{swaf_experiment_name}'", attempts)
    return focus_um * 1e-6


def swaf_find_focus_from_path(czexp_path: Union[str, Path], timeout: int = 30) -> float:
    """
    Run software autofocus from a ``.czexp`` path; return the focus Z in metres.

    The same search as run_swaf_from_path. The differences: metres, and a
    failure raises.

    Args:
        czexp_path: Path to the SWAF ``.czexp`` file.
        timeout: Search timeout in seconds, for ZEN.

    Returns:
        The focus position that ZEN found, in metres.

    Raises:
        FocusSearchFailed: All attempts failed.
    """
    focus_um, attempts = run_swaf_from_path(czexp_path, timeout=timeout)
    if focus_um is None:
        raise FocusSearchFailed("SWAF", f"no focus found with {czexp_path}", attempts)
    return focus_um * 1e-6


def set_objective_set_optovar_sync(objective_nr: int, optovar_nr: int,
                                   timeout_seconds: float = 30.0,
                                   poll_interval: float = OBJECTIVE_POLL_INTERVAL,
                                   max_retries: int = MAX_IMMERSION_RETRIES) -> bool:
    """
    Change the objective and optovar, and wait for the hardware to complete the change.

    Legacy: a timeout is ``False`` in the return value, not an exception. New
    code: set_optics_verified (raises if the optics do not settle).

    Args:
        objective_nr: Objective position (1-4)
        optovar_nr: Optovar position (1-3)
        timeout_seconds: Maximum time to wait for change to complete
        poll_interval: Time between polls of current position
        max_retries: Maximum number of retry attempts for immersion objectives

    Returns:
        True if change completed successfully, False if timeout

    Raises:
        ValueError: If objective_nr or optovar_nr are invalid, or the changer
            reports a position that is not in its inventory (not retried).
    """
    _require_valid(validate_objective_number(objective_nr), "objective number")
    _require_valid(validate_optovar_number(optovar_nr), "optovar number")
    log = set_logging()

    async def _set_and_wait() -> bool:
        # See DEV_NOTES.md "Optics map (this CD7)" for the objective/optovar
        # position -> lens/tubelens mapping (not repeated here to avoid a
        # second copy that can drift if the turret is ever reconfigured).

        # Get current position to detect immersion operations
        current_obj, current_opt = await MS_zenapi_objectivechanger.get_current_objective_and_optovar()

        # Check if moving FROM/TO an immersion objective. Same source as the
        # immersion move-guard: the API immersion type, plus the known slot.
        immersion_positions = await MS_zenapi_stage_LM.immersion_objective_positions()
        is_from_immersion = current_obj in immersion_positions
        is_to_immersion = objective_nr in immersion_positions

        # Adjust timeout for immersion operations
        if is_from_immersion or is_to_immersion:
            # Much longer timeout for immersion operations
            adjusted_timeout = max(timeout_seconds, IMMERSION_OBJECTIVE_TIMEOUT)
            log.info(f"Immersion objective operation detected - using extended timeout: {adjusted_timeout}s")
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
                log.warning(f"Objective change: timeout after {adjusted_timeout}s, "
                            f"retry {retry_count + 1}/{max_retries}")
                retry_count += 1

            except (ValueError, TypeError):
                # Not transient: e.g. the target position is not in the reported
                # inventory (MS_zenapi_objectivechanger._require). A retry cannot
                # fix a configuration mismatch, and a False return would hide it.
                raise
            except Exception as e:
                log.warning(f"Exception during objective change: {e} — "
                            f"retry {retry_count + 1}/{max_retries}")

                # Special handling for the "has not been set" error ZEN raises
                # transiently when moving from immersion (hardware needs time
                # to complete the immersion-removal process).
                if "has not been set" in str(e) and is_from_immersion:
                    log.info("This is expected when moving from 50x immersion - hardware "
                             "needs time. Waiting for immersion removal process...")
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
        objective_nr: Objective position (1-4).
        optovar_nr: Optovar position (1-3).
        settle_s: Wait after the read-back first shows the target.
        timeout_s: Give up if the target is not read back and stable by then.
        poll_s: Interval between read-backs while waiting.

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
        (objective_position, optovar_position)
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
        The sample-carrier name as reported by ZEN.
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


def get_live_scan_configuration() -> Dict[str, Any]:
    """
    Get ZEN's live-scan (tray load / prescan) configuration. Read-only.

    Returns:
        Dict[str, Any] with the keys in MS_zenapi_live_scan.CONFIG_FIELDS
        (sample_holder_template, the prescan flags, refractive_index,
        material). See MS_zenapi_live_scan.get_live_scan_configuration.
    """
    return asyncio.run(MS_zenapi_live_scan.get_live_scan_configuration())


# Dry (air) objective to change to before an eject from the 50x immersion
# objective: Plan-Apochromat 5x/0.35 (DEV_NOTES "Optics map (this CD7)").
EJECT_DRY_OBJECTIVE = 2


def eject_tray(switch_to_dry_objective: bool = True,
               focus_tracker: Optional[DefiniteFocusTracker] = None) -> List[str]:
    """
    Move the sample-carrier tray out (LiveScanService.EjectTray).

    ZEN stops DF and retracts Z itself before it moves the tray, so no Z move is
    done here (seen in the ZEN logs, 2026-11-09; DEV_NOTES "Tray load / eject").
    The eject also works on the 50x immersion objective. The safe procedure is
    to change to the 5x (air) first: this is the default.

    Args:
        switch_to_dry_objective: If the 50x immersion objective is active,
            change to EJECT_DRY_OBJECTIVE (5x) with the current optovar first,
            verified (set_optics_verified). False: eject directly on the 50x.
            No effect on a dry objective.
        focus_tracker: Reset before the eject —
            the last surface belongs to the plate that leaves.

    Returns:
        The notifications ZEN returns.

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

    During a tray load ZEN sends thickness 0 to DF for some seconds, then the
    template or the measured value (DEV_NOTES "Dry objectives: DF finds the
    plate UNDERSIDE"). A DF search in that window parks DF on the plate
    underside. The condition is met when two reads in a row agree:
    the API thickness is more than 0, and the last DF value in the ZEN log (if
    it is in the logs) is more than 0 and agrees with the API.

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

    The prescan steps are those of the current live-scan configuration
    (get_live_scan_configuration). ZEN changes the objective to the 5x
    (position 2) during the load and does not change it back. Set the objective
    again before you acquire. Do not start DF while a load runs. The objective
    and Z after the load are logged. After the load the function waits until
    DF has a non-zero, stable carrier thickness (wait_for_df_bottom).

    Args:
        focus_tracker: Reset before the load —
            the last surface belongs to the previous plate.

    Returns:
        The notifications ZEN returns.

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
        focus_tracker: Reset before the move —
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


def stop_experiment() -> bool:
    """
    Stop the experiment that is active in ZEN, if there is one.

    Use it in the handler of a crash or of Ctrl-C, so that ZEN does not continue
    an acquisition that the pipeline no longer waits for::

        try:
            run_pipeline()
        except (Exception, KeyboardInterrupt):
            log.exception("Run aborted")
            try:
                ms.stop_experiment()
            except Exception:
                log.exception("Could not stop the experiment")
            raise

    Stop ends the acquisition only: the stage, Z and the objective stay where
    they are. See MS_zenapi_experiment_methods.stop_running_experiment (which
    experiment is stopped, and what is not verified on the scope).

    Returns:
        True if an experiment was active and the stop request was sent, False
        if the microscope was idle.

    Raises:
        GRPCError: ZEN refused the status query or the stop request.
    """
    return asyncio.run(MS_zenapi_experiment_methods.stop_running_experiment())


def is_microscope_busy() -> bool:
    """
    Check whether the microscope is currently running an experiment/acquisition.

    Returns:
        True if an experiment or acquisition is running, False if idle.

    Note:
        See get_running_experiment_status for what "busy" covers.
    """
    status = get_running_experiment_status()
    if status is None:
        return False
    return bool(status["is_experiment_running"] or status["is_acquisition_running"])


def preflight(expected_carrier: str, log: logging.Logger,
              stage_speed_percent: Optional[float] = None,
              stage_acceleration_percent: Optional[float] = None,
              expected_material: Optional[str] = None,
              thickness_range_um: Optional[Tuple[float, float]] = None) -> bool:
    """
    Standard start-of-run preflight — run this before ANY hardware action.

    Legacy: a failed check is ``False`` in the return value. New code:
    require_preflight (same checks and arguments, raises PreflightFailed).

    Checks, in order:
      1. the ZEN-API gateway is responsive (a live carrier query that doubles as
         a ping),
      2. the microscope is idle (no experiment already running),
      3. the correct sample carrier is loaded,
      3b. the carrier bottom that DF uses (check_carrier_for_df): thickness not
         0, the material and thickness range if given, DF value = API value,
      4. sets the XY-stage travel speed + acceleration once for the run (device
         state the controller holds across calls) and logs the read-back.

    Also logs, never aborting on them: version identity, the auto-immersion
    refill settings, the live-scan (tray load / prescan) configuration, and
    whether the immersion objectives of the API match the known slot
    (check_immersion_guard).

    Args:
        expected_carrier: The sample-carrier name the run requires; the
            run aborts if a different carrier is loaded.
        log: A logger (anything with .info/.error). Used for all reporting so
            the outcome lands in the per-run log file.
        stage_speed_percent: XY travel speed, percent of max.
            None → STAGE_TRAVEL_SPEED_PERCENT (100 = full speed). Turn down for a
            sensitive/live sample.
        stage_acceleration_percent: XY acceleration, percent of
            max. None → STAGE_ACCELERATION_PERCENT (100 = full).
        expected_material: Carrier bottom material the run
            requires, e.g. "Glass". None: not checked.
        thickness_range_um ((float, float), optional): Allowed carrier bottom
            thickness in µm, e.g. (150, 190). None: not checked.

    Returns:
        True if all checks pass and stage motion was set; False otherwise
        (the reason is logged via ``log.error``). Callers should abort on False.
    """
    # 0. Record version identity: none of this is available over the gRPC API
    # itself (no version/about service), so each piece is recovered out-of-band
    # (see MS_zen_logs.zen_app_version, zeiss_paths.zen_api_gateway_version / zen_api_version /
    # smartmic_version) and logged together so a support question ("what were you
    # running?") has a complete answer: the ZEN application, the gateway service it
    # talks to, the Python-side stubs generated against it, AND the exact SmartMic
    # commit driving the run.
    sm_ver = zeiss_paths.smartmic_version()
    zen_ver = MS_zen_logs.zen_app_version()
    gw_ver = zeiss_paths.zen_api_gateway_version()
    api_ver, api_path = zeiss_paths.zen_api_version()
    log.info(f"SmartMic: {sm_ver or 'unknown (not a git checkout?)'}  |  "
             f"ZEN app: {zen_ver or 'unknown'}  |  "
             f"ZenApiGateway: {gw_ver or 'unknown'}  |  "
             f"zen_api stubs: {api_ver or 'unversioned (loose layout)'} @ {api_path}")

    # 0b. Record the CD7 auto-immersion refill settings (log only, never a
    # reason to abort). Not available over the API either; read from the ZEN
    # user's Options.config and the ZENService log (zeiss_paths, DEV_NOTES
    # "The re-immersion settings are NOT reachable through the ZEN-API").
    try:
        log.info(MS_zen_logs.format_auto_immersion_settings(
            MS_zen_logs.zen_auto_immersion_settings()))
    except Exception as e:
        log.info(f"Auto-immersion refill settings: could not read ({e})")

    # 1. Gateway ping (the carrier query is also the liveness check).
    try:
        carrier = get_sample_carrier_name()
    except Exception as e:
        log.error(f"ZEN-API gateway not responding ({e}) — is ZEN running? Aborting.")
        return False

    # 1b. Record the live-scan (tray load / prescan) configuration: carrier
    # template, bottom material / RI, which prescan steps a load runs. Log only,
    # never a reason to abort; flags a template / carrier mismatch (DEV_NOTES
    # "Tray load / eject").
    try:
        log.info(MS_zenapi_live_scan.format_live_scan_configuration(
            get_live_scan_configuration(), carrier=carrier))
    except Exception as e:
        log.info(f"Live-scan config: could not read ({e})")

    # 1c. Immersion guard: the API immersion types against the known slot.
    # Log only (an error line if they differ); the guard refuses for both.
    check_immersion_guard(log)

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

    # 3b. The carrier bottom that DF uses (thickness, material). A wrong value
    # moves the DF focus on the dry objectives with no error from DF.
    if check_carrier_for_df(log, expected_material, thickness_range_um) is None:
        log.error("DF carrier check failed — aborting.")
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


class _ErrorTap:
    """A logger proxy that also keeps the ``error`` messages (for require_preflight)."""

    def __init__(self, log: logging.Logger) -> None:
        self._log = log
        self.errors: List[str] = []

    def error(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self.errors.append(str(msg))
        self._log.error(msg, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._log, name)


def require_preflight(expected_carrier: str, log: logging.Logger,
                      stage_speed_percent: Optional[float] = None,
                      stage_acceleration_percent: Optional[float] = None,
                      expected_material: Optional[str] = None,
                      thickness_range_um: Optional[Tuple[float, float]] = None) -> None:
    """
    Run the start-of-run preflight; raise if a check fails.

    The same checks, arguments and log lines as preflight. The difference: a
    failed check raises, so a pipeline cannot continue by accident.

    Raises:
        PreflightFailed: A check failed. The message has the reason(s) that
            preflight logged as errors.
    """
    tap = _ErrorTap(log)
    if not preflight(expected_carrier, tap, stage_speed_percent, stage_acceleration_percent,
                     expected_material, thickness_range_um):
        # The last error line is the check that stopped the preflight.
        reason = tap.errors[-1] if tap.errors else "see the run log"
        raise PreflightFailed(f"Preflight failed: {reason}")
