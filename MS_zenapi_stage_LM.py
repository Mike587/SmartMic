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
# This module wraps the ZEN gRPC StageService (lm.hardware.v2). It gives
# helper functions to read and to move the XY stage of a light microscope.
#################################################################

"""Helper functions for the XY stage (``StageService``), and the immersion guard.

A direct XY move lowers Z to 0 first, to prevent a collision. On this inverted
CD7, a Z of 0 breaks the immersion bridge. Thus
``move_stage_to_new_xy_position`` RAISES an exception while an immersion
objective is active. To move the stage in that condition, write the target
position into a ``.czexp`` file and let ZEN run it (DEV_NOTES "inverted CD7 /
Z=0 breaks the immersion bridge").
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

# The settle time after an XY move.
STAGE_SETTLE_SECONDS = 1.0

# The default XY speed and acceleration, as a percent of the maximum [0, 100].
# Use a lower value for a live sample. The acceleration moves the medium, not
# the speed. Set these values with set_stage_motion.
STAGE_TRAVEL_SPEED_PERCENT: float = 100.0
STAGE_ACCELERATION_PERCENT: float = 100.0

# The slot of the 50x/1.2 W objective on this CD7. This constant does not
# depend on the immersion_type from the API. The guard refuses a move if the
# API OR this constant says immersion. Thus a wrong entry in the inventory
# cannot switch the guard off. preflight reports a difference between the two
# sources with check_immersion_guard. Do not change the refusal into a move at
# a "safe" travel Z (DEV_NOTES "inverted CD7 / Z=0 breaks the immersion
# bridge").
IMMERSION_OBJECTIVE_POSITION = 4


class ImmersionObjectiveActive(RuntimeError):
    """The code refused a direct move, because the immersion objective is active.

    This exception has its own type. Thus a caller that tries a move again
    does not try a refusal that the code made on purpose.
    """


async def immersion_objective_positions() -> List[int]:
    """Get the changer positions that the guard counts as immersion, sorted.

    The positions come from the API and from the slot constant.
    """
    api = await MS_zenapi_objectivechanger.get_immersion_objective_positions()
    return sorted(set(api) | {IMMERSION_OBJECTIVE_POSITION})


async def _active_immersion_objective() -> Optional[Any]:
    """Get the active objective if the guard counts it as immersion. If not, ``None``."""
    objective = await MS_zenapi_objectivechanger.get_active_objective()
    if (MS_zenapi_objectivechanger.is_immersion_objective(objective)
            or objective.position == IMMERSION_OBJECTIVE_POSITION):
        return objective
    return None


async def immersion_objective_active() -> bool:
    """Return ``True`` if the active objective is an immersion objective.

    The objective is an immersion objective if its immersion type is not AIR,
    or if it is in slot ``IMMERSION_OBJECTIVE_POSITION``.
    """
    return await _active_immersion_objective() is not None


async def refuse_if_immersion_active(action: str) -> None:
    """Raise ``ImmersionObjectiveActive`` if an immersion objective is active.

    This is the guard for each move that lowers Z to 0 first, because a Z of 0
    breaks the immersion bridge. Run it BEFORE each Z move. *action* gives the
    name of the move in the message.
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
    """Get the current stage position ``[x, y]`` in metres."""
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        simple_stage_service = StageServiceStub(channel=channel, metadata=metadata)

        posXY = await simple_stage_service.get_position(StageServiceGetPositionRequest())
        logger.info(f"Stage XY position: {posXY.x * 1e6:.2f} - {posXY.y * 1e6:.2f} [micron]")

        return [posXY.x, posXY.y]


async def move_stage_to_new_xy_position(x: float, y: float) -> None:
    """Lower Z to 0, move XY to (*x*, *y*) in metres, then wait.

    The wait is ``STAGE_SETTLE_SECONDS``. The function leaves Z at 0. Thus the
    caller must focus again, for example with DF, before it acquires.

    Raises:
        ValueError: ``x`` or ``y`` is not finite, or it is outside the range
            in ``MS_limits``. The function moves nothing.
        ImmersionObjectiveActive (a RuntimeError): An immersion objective is
            active. A move of Z to 0 breaks the immersion bridge. Write the
            target position into a ``.czexp`` file and let ZEN run it.
    """
    logger = set_logging()
    MS_limits.require_xy_position(x, y)

    # The immersion guard must run BEFORE the move of Z to 0 below.
    await refuse_if_immersion_active("stage move")

    async with open_zen_channel(config_path) as (channel, metadata):
        simple_stage_service = StageServiceStub(channel=channel, metadata=metadata)

        # Move Z to 0 before the lateral move, to prevent a collision.
        # Z stays at 0.
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
    """Set the speed and the acceleration of the XY stage.

    Both values are a percent of the maximum, in the range [0, 100]. They are
    device state, and the device keeps them between channels. Thus you set
    them one time for each run, in ``preflight``.

    The function sets the two values SEPARATELY, on purpose. This gateway
    refuses ``SetSpeed`` and ``GetSpeed`` with FAILED_PRECONDITION, but it
    accepts the acceleration. Thus the function writes a failure of the speed
    to the log and continues, but a failure of the acceleration raises an
    exception (DEV_NOTES "Stage speed / acceleration"). Do not make the two
    values dependent on each other again before you test this on the hardware.

    Args:
        speed_percent: ``None`` gives ``STAGE_TRAVEL_SPEED_PERCENT``.
        acceleration_percent: ``None`` gives ``STAGE_ACCELERATION_PERCENT``.

    Returns:
        The values that the function reads back, as percents:
        ``{"speed_x", "speed_y", "acceleration_x", "acceleration_y"}``. The
        two speeds are ``None`` if the device refused the speed.

    Raises:
        ValueError: A percent is not a finite number in [0, 100]. The function
            also refuses ``bool``. It sends nothing to the device.
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

        # The acceleration must succeed. Let a failure go to the caller.
        await stage_service.set_acceleration(
            StageServiceSetAccelerationRequest(acceleration_x=accel, acceleration_y=accel)
        )
        ac = await stage_service.get_acceleration(StageServiceGetAccelerationRequest())

        # This gateway refuses the speed. Continue without it.
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

        # Write the read-back to the log. ZEN or a tile scan can write over
        # these values.
        logger.info(
            f"Stage motion set: speed=({sp_x}, {sp_y})%  "
            f"acceleration=({ac.x}, {ac.y})%"
        )
        return {
            "speed_x": sp_x, "speed_y": sp_y,
            "acceleration_x": ac.x, "acceleration_y": ac.y,
        }
