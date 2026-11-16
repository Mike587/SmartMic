# -*- coding: utf-8 -*-

#################################################################
# File        : MS_CD7_procedures.py
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
The procedures that use more than one device: the preflight and the tray.

``preflight`` does the checks at the start of a run. ``eject_tray`` and
``load_tray_and_prescan`` move the tray with guards: they refuse to move while
an experiment runs, change to a dry objective before an eject, reset the DF
tracker, and wait for the carrier bottom after a load.

Use these functions through ``MS_CD7_API_LoA`` (``import MS_CD7_API_LoA as
ms``). This module calls the layers below as ``MS_CD7_devices.<name>`` and
``MS_definite_focus.<name>``.
"""

import asyncio
import logging
from typing import Dict, List, NoReturn, Optional, Tuple

import zeiss_paths  # noqa: F401  — import FIRST (side effect: extends sys.path for zen_api)
import MS_CD7_devices
import MS_definite_focus
import MS_versions
import MS_zen_logs
import MS_zenapi_live_scan
import MS_zenapi_objectivechanger
from MS_logging import set_logging

# The dry 5x/0.35 objective. Change to it before an eject from the 50x
# immersion objective.
EJECT_DRY_OBJECTIVE = 2


# ---------------------------------------------------------------------------
# The preflight
# ---------------------------------------------------------------------------

class PreflightFailed(RuntimeError):
    """``preflight`` found a reason not to start the run. See the message."""


def _fail(log: logging.Logger, msg: str) -> NoReturn:
    """Write a preflight failure to the log, and raise PreflightFailed with that text."""
    log.error(msg)
    raise PreflightFailed(f"Preflight failed: {msg}")


def _log_versions(log: logging.Logger) -> None:
    """Write the versions of SmartMic, ZEN, the gateway and the stubs to the log.

    The gRPC API has no version service. Thus the code reads the versions from
    other sources.
    """
    smartmic = MS_versions.smartmic_version()
    zen_app = MS_zen_logs.zen_app_version()
    gateway = MS_versions.zen_api_gateway_version()
    stubs, stubs_path = MS_versions.zen_api_version()
    log.info(f"SmartMic: {smartmic or 'unknown (not a git checkout?)'}  |  "
             f"ZEN app: {zen_app or 'unknown'}  |  "
             f"ZenApiGateway: {gateway or 'unknown'}  |  "
             f"zen_api stubs: {stubs or 'unversioned (loose layout)'} @ {stubs_path}")


def _describe_stage_motion(motion: Optional[Dict[str, Optional[float]]]) -> str:
    """Make the text for the read-back of ``set_stage_motion_sync``.

    ``None`` means that the device did not accept the setting.
    """
    if motion is None:
        return "stage motion left as-is (set not supported)"
    acceleration = f"acceleration=({motion['acceleration_x']}, {motion['acceleration_y']})%"
    if motion["speed_x"] is None:
        return (f"{acceleration} "
                f"(speed throttle not supported by this device — acceleration only)")
    return f"stage speed=({motion['speed_x']}, {motion['speed_y']})% {acceleration}"


def check_immersion_guard(log: logging.Logger) -> Optional[bool]:
    """Compare the immersion objectives of the API with the known slot.

    The guard refuses a move of Z to 0 for each source: the immersion type of
    the API, and MS_zenapi_objectivechanger.IMMERSION_OBJECTIVE_POSITION. A difference
    means that someone changed the turret or the constant. The guard then still
    refuses the move for both sources, but the immersion time limits and the
    tray code can be wrong. This function is read-only. Preflight writes the
    result to the log and does not stop the run.

    Returns:
        ``True`` if the two sources agree. ``False`` if they do not agree, and
        the function writes a ``log.error``. ``None`` if the function cannot
        read the inventory, and it writes a ``log.error``.
    """
    slot = MS_zenapi_objectivechanger.IMMERSION_OBJECTIVE_POSITION
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
              f"MS_zenapi_objectivechanger.IMMERSION_OBJECTIVE_POSITION before a run.")
    return False


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
    # 0. The versions. Log only.
    _log_versions(log)

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
        carrier = MS_CD7_devices.get_sample_carrier_name()
    except Exception as e:
        _fail(log, f"ZEN-API gateway not responding ({e}) — is ZEN running? Aborting.")

    # 1b. The live-scan configuration. Log only. It shows a difference
    #     between the template and the carrier (DEV_NOTES "Tray load /
    #     eject").
    try:
        log.info(MS_zenapi_live_scan.format_live_scan_configuration(
            MS_CD7_devices.get_live_scan_configuration(), carrier=carrier))
    except Exception as e:
        log.info(f"Live-scan config: could not read ({e})")

    # 1c. Do the two sources of the immersion guard agree? Log only. The
    #     guard refuses a move for both sources.
    check_immersion_guard(log)

    # 2. The microscope must be idle.
    try:
        busy = MS_CD7_devices.is_microscope_busy()
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
    if MS_definite_focus.check_carrier_for_df(log, expected_material, thickness_range_um) is None:
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
        motion = MS_CD7_devices.set_stage_motion_sync(stage_speed_percent,
                                                      stage_acceleration_percent)
    except Exception as e:
        if throttle_requested:
            _fail(log, f"Could not set requested stage acceleration ({e}) — aborting.")
        log.warning(f"Stage acceleration not set ({e}); the device may not support "
                    f"this knob. No throttle was requested, so proceeding at the stage's "
                    f"current motion settings (full speed).")

    log.info(f"Preflight OK: gateway responsive, microscope idle, "
             f"carrier '{carrier}', {_describe_stage_motion(motion)}.")


# ---------------------------------------------------------------------------
# The tray
# ---------------------------------------------------------------------------

def eject_tray(switch_to_dry_objective: bool = True,
               focus_tracker: Optional[MS_definite_focus.DefiniteFocusTracker] = None) -> List[str]:
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
    if MS_CD7_devices.is_microscope_busy():
        raise RuntimeError("Microscope is busy (an experiment runs) — tray not ejected.")
    if focus_tracker is not None:
        focus_tracker.reset()
    if switch_to_dry_objective and MS_CD7_devices.immersion_objective_active():
        _, optovar = MS_CD7_devices.get_current_objective_and_optovar()
        log.info(f"Eject: immersion objective active — changing to objective "
                 f"{EJECT_DRY_OBJECTIVE} (5x) first.")
        # This raises RuntimeError if the optics do not settle. Then no eject occurs.
        MS_CD7_devices.set_optics_verified(EJECT_DRY_OBJECTIVE, optovar)
    return asyncio.run(MS_zenapi_live_scan.eject_tray())


def load_tray_and_prescan(
    focus_tracker: Optional[MS_definite_focus.DefiniteFocusTracker] = None,
) -> List[str]:
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
    if MS_CD7_devices.is_microscope_busy():
        raise RuntimeError("Microscope is busy (an experiment runs) — tray not loaded.")
    if focus_tracker is not None:
        focus_tracker.reset()
    notifications = asyncio.run(MS_zenapi_live_scan.load_tray_and_prescan())
    MS_definite_focus.wait_for_df_bottom()
    try:
        objective, optovar = MS_CD7_devices.get_current_objective_and_optovar()
        z_um = MS_CD7_devices.get_current_z_position() * 1e6
        carrier = MS_CD7_devices.get_sample_carrier_name()
        log.info(f"After load: objective {objective}, optovar {optovar}, Z {z_um:.1f} µm, "
                 f"carrier '{carrier}'. Set the objective before you acquire.")
    except Exception as e:
        log.warning(f"After load: could not read the objective / Z / carrier ({e}).")
    return notifications
