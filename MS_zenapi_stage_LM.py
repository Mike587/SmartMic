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

"""
XY stage helpers for ZEN blue / ZEN core via gRPC.

This module wraps the ZEN gRPC ``StageService`` (lm.hardware.v2) and
integrates with :mod:`MS_zenapi_focus` to lower the objective before any XY
move so that the objective cannot collide with the sample when traversing
large distances.

That Z=0 lower would break the immersion bridge on this inverted CD7, so
``move_stage_to_new_xy_position`` refuses to run while the immersion objective
(:data:`IMMERSION_OBJECTIVE_POSITION`) is active — drive the stage via a
positioned ``.czexp`` run by ZEN in that case (see DEV_NOTES).

Public functions
----------------
get_current_xy_stage_coordinates  -- Read the current XY position in metres
move_stage_to_new_xy_position      -- Lower Z, move XY, then wait for settling
set_stage_motion                   -- Set XY travel speed + acceleration (% [0,100])
"""

import asyncio
import numpy as np
import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
from MS_zenapi_helpers import set_logging, open_zen_channel
import MS_zenapi_focus
import MS_zenapi_objectivechanger

# Auto-generated gRPC stubs for the XY stage service.
from zen_api.lm.hardware.v2 import (
    StageServiceStub,
    StageServiceGetPositionRequest,
    StageServiceMoveToRequest,
    StageServiceSetSpeedRequest,
    StageServiceSetAccelerationRequest,
    StageServiceGetSpeedRequest,
    StageServiceGetAccelerationRequest,
)

# config.ini path — single-sourced from zeiss_paths (repo root), not recomputed.
from zeiss_paths import CONFIG_PATH as config_path

# Seconds to wait after an XY move so the stage settles mechanically before the
# caller proceeds (e.g. to autofocus). 1 s is enough on this system.
STAGE_SETTLE_SECONDS = 1.0

# Default XY-stage travel speed and acceleration, percent of max ([0, 100]).
# Full speed/acceleration is the normal-project default (fast). These are the
# single source of truth — the "global stage-speed control for a whole project"
# knob: turn them DOWN for a sensitive/live sample. Set both together via
# set_stage_motion(); for live samples it is the ACCELERATION (jerk at move
# start/stop) that sloshes the medium, so a low speed paired with a high
# acceleration would still disturb the sample.
STAGE_TRAVEL_SPEED_PERCENT: float = 100.0
STAGE_ACCELERATION_PERCENT: float = 100.0

# Objective-changer position of the immersion objective (50x/1.2) — the only
# immersion objective on this scope. A direct stage move lowers Z to 0, which on
# this inverted CD7 retracts the objective and breaks the immersion water bridge,
# so move_stage_to_new_xy_position REFUSES to run while it is active (raises). We
# deliberately do not reimplement an immersion-safe move: choosing a safe non-zero
# travel Z is delicate geometry ZEN already does inside an experiment. With the
# immersion objective active, drive the stage via a positioned .czexp run by ZEN.
# See DEV_NOTES "inverted CD7 / Z=0 breaks the immersion bridge".
IMMERSION_OBJECTIVE_POSITION = 4


async def get_current_xy_stage_coordinates():
    """Return the current XY stage position in metres.

    Args:
        None

    Returns:
        List ``[x, y]`` with both values in metres.
    """
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        simple_stage_service = StageServiceStub(channel=channel, metadata=metadata)

        # StageService returns positions in metres; convert to µm for the log only.
        posXY = await simple_stage_service.get_position(StageServiceGetPositionRequest())
        logger.info(
            f"Stage XY Position 1: "
            f"{np.round(posXY.x * 1e6, 2)} - {np.round(posXY.y * 1e6, 2)} [micron]"
        )

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

    Raises:
        RuntimeError: If the immersion objective (changer position
            ``IMMERSION_OBJECTIVE_POSITION``) is active. The Z=0 collision-safety
            lower would break the immersion bridge on this inverted CD7, so a
            direct move is refused — drive the stage via a positioned ``.czexp``
            run by ZEN instead.

    Notes:
        After the move the function waits ``STAGE_SETTLE_SECONDS`` to allow the
        stage to mechanically settle before the caller proceeds.
        The Z-drive is left at 0 m on return — it is NOT moved back to its
        pre-move position. Callers must establish focus at the new position
        (e.g. via Definite Focus) before acquiring.
    """
    logger = set_logging()

    # Immersion guard — must run BEFORE the Z=0 lower below. With the immersion
    # objective active, lowering Z to 0 retracts the objective and breaks the
    # immersion water bridge on this inverted CD7 (and DF then fails). Refuse the
    # direct move rather than kill immersion; the caller should write the target
    # XY(Z) into a .czexp and let ZEN drive the stage (its immersion logic
    # raises/lowers Z safely). See DEV_NOTES.
    obj_position, _ = await MS_zenapi_objectivechanger.get_current_objective_and_optovar()
    if obj_position == IMMERSION_OBJECTIVE_POSITION:
        raise RuntimeError(
            f"Refusing direct stage move: the immersion objective (changer "
            f"position {IMMERSION_OBJECTIVE_POSITION}) is active. "
            f"move_stage_to_new_xy_position lowers Z to 0, which breaks the "
            f"immersion bridge on this inverted CD7. Drive the stage by writing "
            f"the target XY(Z) into a .czexp and running it via ZEN instead."
        )

    async with open_zen_channel(config_path) as (channel, metadata):
        simple_stage_service = StageServiceStub(channel=channel, metadata=metadata)

        # Lower the objective to Z=0 before moving laterally to prevent
        # collisions. The Z-drive is deliberately left at 0 afterwards; the
        # caller re-focuses (e.g. Definite Focus) at the new position.
        new_z = 0
        await MS_zenapi_focus.move_focus_to_new_z_position(new_z)

        posXY = await simple_stage_service.get_position(StageServiceGetPositionRequest())
        logger.info(
            f"Stage XY Position 1: "
            f"{np.round(posXY.x * 1e6, 2)} - {np.round(posXY.y * 1e6, 2)} [micron]"
        )

        # Perform the XY move (coordinates in metres).
        await simple_stage_service.move_to(StageServiceMoveToRequest(x=x, y=y))

        new_posXY = await simple_stage_service.get_position(StageServiceGetPositionRequest())
        logger.info(
            f"Stage XY Position 2: "
            f"{np.round(new_posXY.x * 1e6, 2)} - {np.round(new_posXY.y * 1e6, 2)} [micron]"
        )

        # Allow the stage to settle mechanically before the next operation.
        logger.info(f"Waiting for {STAGE_SETTLE_SECONDS} seconds...")
        await asyncio.sleep(STAGE_SETTLE_SECONDS)


async def set_stage_motion(speed_percent: float = None,
                           acceleration_percent: float = None) -> dict:
    """Set the XY-stage travel speed and acceleration (percent of max, [0, 100]).

    Speed/acceleration are device state, NOT per-move parameters: ``MoveTo`` has
    no velocity field, and the controller holds the values across SmartMic's
    per-call open/close of the gRPC channel. So this is the "global stage-speed
    control for a whole project" knob — set it once at the start of a run (see
    the shared ``preflight``) and leave it; every later move inherits it. No
    restore (restoring would defeat the purpose).

    Both are set TOGETHER on purpose: for a live sample it is the acceleration
    (the jerk at the start/stop of a move) that sloshes the medium, so a low top
    speed paired with a high acceleration would still jolt the sample.

    Args:
        speed_percent: Target XY travel speed, percent of max. ``None`` falls
            back to ``STAGE_TRAVEL_SPEED_PERCENT`` (100 = full speed).
        acceleration_percent: Target XY acceleration, percent of max. ``None``
            falls back to ``STAGE_ACCELERATION_PERCENT`` (100 = full).

    Returns:
        The read-back after setting, as
        ``{"speed_x", "speed_y", "acceleration_x", "acceleration_y"}`` (percent).
    """
    logger = set_logging()
    speed = STAGE_TRAVEL_SPEED_PERCENT if speed_percent is None else speed_percent
    accel = STAGE_ACCELERATION_PERCENT if acceleration_percent is None else acceleration_percent

    async with open_zen_channel(config_path) as (channel, metadata):
        stage_service = StageServiceStub(channel=channel, metadata=metadata)

        # Set acceleration AND speed (both axes). Order is not significant —
        # neither takes effect until the next move — but set both so the pair is
        # always consistent.
        await stage_service.set_acceleration(
            StageServiceSetAccelerationRequest(acceleration_x=accel, acceleration_y=accel)
        )
        await stage_service.set_speed(
            StageServiceSetSpeedRequest(speed_x=speed, speed_y=speed)
        )

        # Read back what is actually in effect, so the run log records the real
        # values (insurance against a silent overwrite by ZEN / a tile scan).
        sp = await stage_service.get_speed(StageServiceGetSpeedRequest())
        ac = await stage_service.get_acceleration(StageServiceGetAccelerationRequest())
        logger.info(
            f"Stage motion set: speed=({sp.x}, {sp.y})%  "
            f"acceleration=({ac.x}, {ac.y})%"
        )
        return {
            "speed_x": sp.x, "speed_y": sp.y,
            "acceleration_x": ac.x, "acceleration_y": ac.y,
        }
