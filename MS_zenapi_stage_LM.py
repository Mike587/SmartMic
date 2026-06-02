# -*- coding: utf-8 -*-

#################################################################
# File        : zenapi_stage_LM.py
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

"""
XY stage helpers for ZEN blue / ZEN core via gRPC.

This module wraps the ZEN gRPC ``StageService`` (lm.hardware.v2) and
integrates with :mod:`MS_zenapi_focus` to lower the objective before any XY
move so that the objective cannot collide with the sample when traversing
large distances.

Public functions
----------------
get_current_xy_stage_coordinates  -- Read the current XY position in metres
move_stage_to_new_xy_position      -- Lower Z, move XY, then wait for settling
"""

import asyncio
from pathlib import Path
import numpy as np
import sys
import zeiss_paths  # noqa: F401  — extends sys.path so zen_api / zen_api_utils resolve
from zen_api_utils.misc import set_logging, initialize_zenapi
import time
import MS_zenapi_focus

# Custom stage helper utilities (wraps the raw gRPC position structs).
from zen_api_utils.stage import (
    get_stageXY_position_simple,
    move_to_stageXY_position_simple,
    StageXYPosition,
)

# Auto-generated gRPC stubs for the XY stage service.
from zen_api.lm.hardware.v2 import (
    StageServiceStub,
    StageServiceGetPositionRequest,
    StageServiceMoveToRequest,
)

# Resolve config.ini relative to this script so the module works regardless
# of the current working directory.
script_dir = Path(__file__).parent
config_path = script_dir / "config.ini"


async def get_current_xy_stage_coordinates():
    """Return the current XY stage position in metres.

    Args:
        None

    Returns:
        List ``[x, y]`` with both values in metres.
    """
    logger = set_logging()

    channel, metadata = initialize_zenapi(config_path)
    simple_stage_service = StageServiceStub(channel=channel, metadata=metadata)

    # StageService returns positions in metres; convert to µm for the log only.
    posXY = await simple_stage_service.get_position(StageServiceGetPositionRequest())
    logger.info(
        f"Stage XY Position 1: "
        f"{np.round(posXY.x * 1e6, 2)} - {np.round(posXY.y * 1e6, 2)} [micron]"
    )

    channel.close()
    return [posXY.x, posXY.y]


async def move_stage_to_new_xy_position(x: float, y: float):
    """Move the XY stage to an absolute position.

    Before moving, the Z-drive is lowered to 0 m so that the objective
    does not collide with the sample edge or well-plate walls during a
    large lateral displacement.

    Args:
        x: Target X position in metres.
        y: Target Y position in metres.

    Returns:
        None

    Notes:
        After the move the function waits 3 seconds to allow the stage
        to mechanically settle before the caller proceeds.
        The previous Z position is intentionally NOT restored here;
        callers must re-focus (e.g. via Definite Focus) after moving.
    """
    logger = set_logging()

    channel, metadata = initialize_zenapi(config_path)
    simple_stage_service = StageServiceStub(channel=channel, metadata=metadata)

    # Lower the objective to Z=0 before moving laterally to prevent collisions.
    # TODO: restore original Z position after the move once collision-safe
    # Z management is fully handled by the pipeline.
    # z = asyncio.run(MS_zenapi_focus.get_current_z_focus_position())
    new_z = 0
    await MS_zenapi_focus.move_focus_to_new_z_position(new_z)

    posXY = await simple_stage_service.get_position(StageServiceGetPositionRequest())
    logger.info(
        f"Stage XY Position 1: "
        f"{np.round(posXY.x * 1e6, 2)} - {np.round(posXY.y * 1e6, 2)} [micron]"
    )

    # Perform the XY move (coordinates in metres).
    new_posx = x
    new_posy = y
    await simple_stage_service.move_to(StageServiceMoveToRequest(x=new_posx, y=new_posy))

    new_posXY = await simple_stage_service.get_position(StageServiceGetPositionRequest())
    logger.info(
        f"Stage XY Position 2: "
        f"{np.round(new_posXY.x * 1e6, 2)} - {np.round(new_posXY.y * 1e6, 2)} [micron]"
    )

    # Restore Z to pre-move position (disabled; kept as reference).
    # asyncio.run(MS_zenapi_focus.move_focus_to_new_z_position(z))

    # Allow the stage to settle mechanically before the next operation.
    logger.info("Waiting for 3 seconds...")
    time.sleep(3)

    # Alternative move pattern using the zen_api_utils helper wrappers.
    # Kept here as a usage reference; not executed during normal operation.
    '''
    new_posx = 103500 * 1e-6
    new_posy = 71500 * 1e-6
    await move_to_stageXY_position_simple(
        simple_stage_service, stage_positionXY=StageXYPosition(new_posx, new_posy)
    )
    new_posXY = await get_stageXY_position_simple(simple_stage_service)
    logger.info(
        f"Stage XY Position 2: "
        f"{np.round(new_posXY.x * 1e6, 2)} - {np.round(new_posXY.y * 1e6, 2)} [micron]"
    )

    logger.info("Waiting for 3 seconds...")
    time.sleep(3)

    # Move back to the initial position.
    await simple_stage_service.move_to(StageServiceMoveToRequest(x=posXY.x, y=posXY.y))
    posXY = await simple_stage_service.get_position(StageServiceGetPositionRequest())
    logger.info(
        f"Stage XY Position 1: "
        f"{np.round(posXY.x * 1e6, 2)} - {np.round(posXY.y * 1e6, 2)} [micron]"
    )
    '''

    channel.close()


if __name__ == "__main__":
    logger = set_logging()
    asyncio.run(main(sys.argv))
