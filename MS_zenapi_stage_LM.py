# -*- coding: utf-8 -*-

#################################################################
# Based on    : zenapi_stage_LM.py
# Author      : SRh
# Institution : Carl Zeiss Microscopy GmbH
#
# Copyright(c) 2025 Carl Zeiss AG, Germany. All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

#################################################################
# File        : MS_zenapi_stage_LM.py
# Modified by : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#
# Wraps the ZEN gRPC StageService (lm.hardware.v2) to provide helpers
# for reading and moving the XY stage on a light-microscopy system.
#################################################################

"""XY stage helpers (StageService) and the immersion guard.

A direct XY move lowers Z to 0 first (collision safety). On this inverted CD7
that breaks the immersion bridge, so ``move_stage_to_new_xy_position`` RAISES
while an immersion objective is active: drive the stage via a positioned
``.czexp`` run by ZEN instead (DEV_NOTES "inverted CD7 / Z=0 breaks the
immersion bridge").
"""

import asyncio
import math
from typing import Any, Dict, List, Optional

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
from MS_zenapi_helpers import set_logging, open_zen_channel
import MS_limits
import MS_zenapi_focus
import MS_zenapi_objectivechanger

from zen_api.lm.hardware.v2 import (
    StageServiceStub,
    StageServiceGetPositionRequest,
    StageServiceMoveToRequest,
    StageServiceSetSpeedRequest,
    StageServiceSetAccelerationRequest,
    StageServiceGetSpeedRequest,
    StageServiceGetAccelerationRequest,
)

from zeiss_paths import CONFIG_PATH as config_path

# Settle time after an XY move.
STAGE_SETTLE_SECONDS = 1.0

# Default XY speed / acceleration, percent of max [0, 100]. Lower for a live
# sample; acceleration is what sloshes the medium (set via set_stage_motion).
STAGE_TRAVEL_SPEED_PERCENT: float = 100.0
STAGE_ACCELERATION_PERCENT: float = 100.0

# Slot of the 50x/1.2 W on this CD7. Independent of the API immersion_type: the
# guard refuses if EITHER says immersion, so a wrong inventory entry cannot
# switch it off (preflight reports a mismatch: check_immersion_guard). Do not
# replace the refusal with a move at a "safe" travel Z (DEV_NOTES "inverted
# CD7 / Z=0 breaks the immersion bridge").
IMMERSION_OBJECTIVE_POSITION = 4


class ImmersionObjectiveActive(RuntimeError):
    """A direct move was refused because the immersion objective is active.

    Its own type so retrying callers never retry a deliberate refusal.
    """


async def immersion_objective_positions() -> List[int]:
    """Sorted changer positions the guard treats as immersion (API + slot constant)."""
    api = await MS_zenapi_objectivechanger.get_immersion_objective_positions()
    return sorted(set(api) | {IMMERSION_OBJECTIVE_POSITION})


async def _active_immersion_objective() -> Optional[Any]:
    """The active objective if the guard treats it as immersion, else None."""
    objective = await MS_zenapi_objectivechanger.get_active_objective()
    if (MS_zenapi_objectivechanger.is_immersion_objective(objective)
            or objective.position == IMMERSION_OBJECTIVE_POSITION):
        return objective
    return None


async def immersion_objective_active() -> bool:
    """True if the active objective has a non-AIR immersion type or is in
    slot ``IMMERSION_OBJECTIVE_POSITION``."""
    return await _active_immersion_objective() is not None


async def refuse_if_immersion_active(action: str) -> None:
    """Raise ImmersionObjectiveActive if an immersion objective is active.

    Guard for every move that first lowers Z to 0 (breaks the immersion
    bridge). Run it BEFORE any Z move. *action* names the move in the message.
    """
    objective = await _active_immersion_objective()
    if objective is not None:
        raise ImmersionObjectiveActive(
            f"Refusing direct {action}: the immersion objective "
            f"'{objective.name}' (changer position {objective.position}) is "
            f"active. The {action} "
            f"lowers Z to 0, which breaks the immersion bridge on this inverted "
            f"CD7. Drive the stage by writing the target XY(Z) into a .czexp and "
            f"running it via ZEN instead."
        )


async def get_current_xy_stage_coordinates() -> List[float]:
    """Return the current stage position ``[x, y]`` in metres."""
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        simple_stage_service = StageServiceStub(channel=channel, metadata=metadata)

        posXY = await simple_stage_service.get_position(StageServiceGetPositionRequest())
        logger.info(f"Stage XY position: {posXY.x * 1e6:.2f} - {posXY.y * 1e6:.2f} [micron]")

        return [posXY.x, posXY.y]


async def move_stage_to_new_xy_position(x: float, y: float) -> None:
    """Lower Z to 0, move XY to (*x*, *y*) in metres, wait ``STAGE_SETTLE_SECONDS``.

    Z is left at 0: the caller must re-focus (e.g. DF) before acquiring.

    Raises:
        ValueError: ``x``/``y`` not finite / outside ``MS_limits``. Nothing is moved.
        ImmersionObjectiveActive (a RuntimeError): An immersion objective is
            active; the Z=0 lower would break the immersion bridge. Use a
            positioned ``.czexp`` run by ZEN instead.
    """
    logger = set_logging()
    MS_limits.require_xy_position(x, y)

    # Immersion guard: must run BEFORE the Z=0 lower below.
    await refuse_if_immersion_active("stage move")

    async with open_zen_channel(config_path) as (channel, metadata):
        simple_stage_service = StageServiceStub(channel=channel, metadata=metadata)

        # Z=0 before the lateral move (collision safety); left there.
        new_z = 0
        await MS_zenapi_focus.move_focus_to_new_z_position(new_z)

        posXY = await simple_stage_service.get_position(StageServiceGetPositionRequest())
        logger.info(
            f"Stage XY position before the move: "
            f"{posXY.x * 1e6:.2f} - {posXY.y * 1e6:.2f} [micron]"
        )

        await simple_stage_service.move_to(StageServiceMoveToRequest(x=x, y=y))

        new_posXY = await simple_stage_service.get_position(StageServiceGetPositionRequest())
        logger.info(
            f"Stage XY position after the move: "
            f"{new_posXY.x * 1e6:.2f} - {new_posXY.y * 1e6:.2f} [micron]"
        )

        logger.info(f"Waiting for {STAGE_SETTLE_SECONDS} seconds...")
        await asyncio.sleep(STAGE_SETTLE_SECONDS)


async def set_stage_motion(speed_percent: Optional[float] = None,
                           acceleration_percent: Optional[float] = None,
                           ) -> Dict[str, Optional[float]]:
    """Set the XY-stage speed and acceleration (percent of max, [0, 100]).

    Device state, held across channels: set once per run (``preflight``).
    Applied INDEPENDENTLY on purpose: this gateway rejects
    ``SetSpeed``/``GetSpeed`` (FAILED_PRECONDITION) but accepts acceleration,
    so a speed failure is logged and tolerated; an acceleration failure raises
    (DEV_NOTES "Stage speed / acceleration"). Do not re-couple without
    re-testing on the hardware.

    Args:
        speed_percent: ``None`` = ``STAGE_TRAVEL_SPEED_PERCENT``.
        acceleration_percent: ``None`` = ``STAGE_ACCELERATION_PERCENT``.

    Returns:
        Read-back ``{"speed_x", "speed_y", "acceleration_x", "acceleration_y"}``
        (percent); the speeds are ``None`` if the device rejected speed.

    Raises:
        ValueError: A percent is not a finite number in [0, 100] (bool is
            refused). Nothing is sent to the device.
    """
    logger = set_logging()
    speed = STAGE_TRAVEL_SPEED_PERCENT if speed_percent is None else speed_percent
    accel = STAGE_ACCELERATION_PERCENT if acceleration_percent is None else acceleration_percent
    for name, value in (("speed_percent", speed), ("acceleration_percent", accel)):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value <= 100):
            raise ValueError(f"{name} must be a finite number in [0, 100], got {value!r}")

    async with open_zen_channel(config_path) as (channel, metadata):
        stage_service = StageServiceStub(channel=channel, metadata=metadata)

        # Acceleration must succeed: let a failure propagate.
        await stage_service.set_acceleration(
            StageServiceSetAccelerationRequest(acceleration_x=accel, acceleration_y=accel)
        )
        ac = await stage_service.get_acceleration(StageServiceGetAccelerationRequest())

        # Speed is rejected by this gateway: tolerate it.
        sp_x = sp_y = None
        try:
            await stage_service.set_speed(
                StageServiceSetSpeedRequest(speed_x=speed, speed_y=speed)
            )
            sp = await stage_service.get_speed(StageServiceGetSpeedRequest())
            sp_x, sp_y = sp.x, sp.y
        except Exception as e:
            logger.warning(
                f"Stage speed not supported by this device/gateway ({e}); "
                f"proceeding with acceleration only."
            )

        # Log the read-back (ZEN / a tile scan can overwrite the values).
        logger.info(
            f"Stage motion set: speed=({sp_x}, {sp_y})%  "
            f"acceleration=({ac.x}, {ac.y})%"
        )
        return {
            "speed_x": sp_x, "speed_y": sp_y,
            "acceleration_x": ac.x, "acceleration_y": ac.y,
        }
