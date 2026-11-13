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

"""Helper functions for the XY stage (``StageService``).

A direct XY move lowers Z to 0 first, to prevent a collision. On this inverted
CD7, a Z of 0 breaks the immersion bridge. Thus
``move_stage_to_new_xy_position`` RAISES an exception while an immersion
objective is active. The guard is in ``MS_zenapi_objectivechanger``. To move
the stage in that condition, write the target position into a ``.czexp`` file
and let ZEN run it (DEV_NOTES "inverted CD7 / Z=0 breaks the immersion
bridge").
"""

import asyncio
import math
from typing import Any, Dict, List, Optional

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
from MS_logging import set_logging
from MS_zenapi_helpers import open_zen_channel
import MS_limits
import MS_zenapi_focus
import MS_zenapi_objectivechanger

# The immersion guard is in MS_zenapi_objectivechanger. These names stay
# available here for the code that uses them from this module. The constant is
# a copy. Change it in MS_zenapi_objectivechanger.
from MS_zenapi_objectivechanger import (  # noqa: F401
    IMMERSION_OBJECTIVE_POSITION,
    ImmersionObjectiveActive,
    immersion_objective_active,
    immersion_objective_positions,
    refuse_if_immersion_active,
)

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


def _xy_um(position: Any) -> str:
    """Make the log text of a stage position, in µm."""
    return f"{position.x * 1e6:.2f} - {position.y * 1e6:.2f} [micron]"


async def get_current_xy_stage_coordinates() -> List[float]:
    """Get the current stage position ``[x, y]`` in metres."""
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        stage_service = StageServiceStub(channel=channel, metadata=metadata)

        position = await stage_service.get_position(StageServiceGetPositionRequest())
        logger.info(f"Stage XY position: {_xy_um(position)}")

        return [position.x, position.y]


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
    await MS_zenapi_objectivechanger.refuse_if_immersion_active("stage move")

    async with open_zen_channel(config_path) as (channel, metadata):
        stage_service = StageServiceStub(channel=channel, metadata=metadata)

        # Move Z to 0 before the lateral move, to prevent a collision.
        # Z stays at 0.
        await MS_zenapi_focus.move_focus_to_new_z_position(0)

        before = await stage_service.get_position(StageServiceGetPositionRequest())
        logger.info(f"Stage XY position before the move: {_xy_um(before)}")

        await stage_service.move_to(StageServiceMoveToRequest(x=x, y=y))

        after = await stage_service.get_position(StageServiceGetPositionRequest())
        logger.info(f"Stage XY position after the move: {_xy_um(after)}")

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
    acceleration = (STAGE_ACCELERATION_PERCENT if acceleration_percent is None
                    else acceleration_percent)
    for name, value in (("speed_percent", speed), ("acceleration_percent", acceleration)):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 <= value <= 100):
            raise ValueError(f"{name} must be a finite number in [0, 100], got {value!r}")

    async with open_zen_channel(config_path) as (channel, metadata):
        stage_service = StageServiceStub(channel=channel, metadata=metadata)

        # The acceleration must succeed. Let a failure go to the caller.
        await stage_service.set_acceleration(StageServiceSetAccelerationRequest(
            acceleration_x=acceleration, acceleration_y=acceleration))
        acceleration_readback = await stage_service.get_acceleration(
            StageServiceGetAccelerationRequest())

        # This gateway refuses the speed. Continue without it.
        speed_x = speed_y = None
        try:
            await stage_service.set_speed(
                StageServiceSetSpeedRequest(speed_x=speed, speed_y=speed))
            speed_readback = await stage_service.get_speed(StageServiceGetSpeedRequest())
            speed_x, speed_y = speed_readback.x, speed_readback.y
        except Exception as e:
            logger.warning(
                f"Stage speed not supported by this device/gateway ({e}); "
                f"proceeding with acceleration only."
            )

        # Write the read-back to the log. ZEN or a tile scan can write over
        # these values.
        logger.info(
            f"Stage motion set: speed=({speed_x}, {speed_y})%  "
            f"acceleration=({acceleration_readback.x}, {acceleration_readback.y})%"
        )
        return {
            "speed_x": speed_x,
            "speed_y": speed_y,
            "acceleration_x": acceleration_readback.x,
            "acceleration_y": acceleration_readback.y,
        }
