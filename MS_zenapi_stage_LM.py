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
refuse_if_immersion_active         -- Immersion guard shared by the Z=0-lowering moves
"""

import asyncio
import math
from typing import Any, Dict, List, Optional

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
from MS_zenapi_helpers import set_logging, open_zen_channel
import MS_limits
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

# A direct stage move lowers Z to 0, which on this inverted CD7 retracts the
# objective and breaks the immersion water bridge, so
# move_stage_to_new_xy_position REFUSES to run while an immersion objective is
# active (raises). We deliberately do not reimplement an immersion-safe move:
# choosing a safe non-zero travel Z is delicate geometry ZEN already does inside
# an experiment. With an immersion objective active, drive the stage via a
# positioned .czexp run by ZEN.
# See DEV_NOTES "inverted CD7 / Z=0 breaks the immersion bridge".
#
# Which objective is an immersion objective comes from the API
# (ObjectiveData.immersion_type, see immersion_objective_active). This constant
# is the known slot of the 50x/1.2 W on this CD7. It is a second, independent
# condition: the guard refuses if EITHER says "immersion", so a wrong inventory
# entry or a changed turret cannot switch the guard off. preflight reports a
# difference between the two (MS_CD7_API_LoA.check_immersion_guard).
IMMERSION_OBJECTIVE_POSITION = 4


class ImmersionObjectiveActive(RuntimeError):
    """A direct move was refused because the immersion objective is active.

    Its own type so callers that retry failed moves (e.g. the LoA's tolerant
    moves) can tell a deliberate refusal from a hardware error and never retry it.
    """


async def immersion_objective_positions() -> List[int]:
    """Changer positions the guard treats as immersion (sorted).

    The positions the API reports as immersion, plus
    ``IMMERSION_OBJECTIVE_POSITION``.
    """
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
    """True if the active objective is an immersion objective.

    True if the API gives it an immersion type other than AIR
    (``MS_zenapi_objectivechanger.is_immersion_objective``), or if it is in
    slot ``IMMERSION_OBJECTIVE_POSITION``.
    """
    return await _active_immersion_objective() is not None


async def refuse_if_immersion_active(action: str) -> None:
    """Raise ImmersionObjectiveActive if an immersion objective is active.

    Shared guard for every library move that first lowers Z to 0 (the XY move,
    the container move): on this inverted CD7 that retracts the objective and
    breaks the immersion water bridge. Run it BEFORE any Z move.

    Args:
        action: What is being refused, for the error message (e.g. "stage move").
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
    """Return the current XY stage position in metres.
    Returns:
        List ``[x, y]`` with both values in metres.
    """
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        simple_stage_service = StageServiceStub(channel=channel, metadata=metadata)

        # StageService returns positions in metres; convert to µm for the log only.
        posXY = await simple_stage_service.get_position(StageServiceGetPositionRequest())
        logger.info(f"Stage XY position: {posXY.x * 1e6:.2f} - {posXY.y * 1e6:.2f} [micron]")

        return [posXY.x, posXY.y]


async def move_stage_to_new_xy_position(x: float, y: float) -> None:
    """Move the XY stage to an absolute position.

    Before moving, the Z-drive is lowered to 0 m so that the objective
    does not collide with the sample edge or well-plate walls during a
    large lateral displacement.

    Args:
        x: Target X position in metres.
        y: Target Y position in metres.
    Raises:
        ValueError: ``x`` or ``y`` is not a finite number within the stage
            limits (``MS_limits``). Nothing is moved.
        ImmersionObjectiveActive (a RuntimeError): If an immersion objective
            (``immersion_objective_active``) is active. The
            Z=0 collision-safety lower would break the immersion bridge on this
            inverted CD7, so a direct move is refused — drive the stage via a
            positioned ``.czexp`` run by ZEN instead.

    Notes:
        After the move the function waits ``STAGE_SETTLE_SECONDS`` to allow the
        stage to mechanically settle before the caller proceeds.
        The Z-drive is left at 0 m on return — it is NOT moved back to its
        pre-move position. Callers must establish focus at the new position
        (e.g. via Definite Focus) before acquiring.
    """
    logger = set_logging()
    MS_limits.require_xy_position(x, y)

    # Immersion guard — must run BEFORE the Z=0 lower below. With the immersion
    # objective active, lowering Z to 0 retracts the objective and breaks the
    # immersion water bridge on this inverted CD7 (and DF then fails). Refuse the
    # direct move rather than kill immersion; the caller should write the target
    # XY(Z) into a .czexp and let ZEN drive the stage (its immersion logic
    # raises/lowers Z safely). See DEV_NOTES.
    await refuse_if_immersion_active("stage move")

    async with open_zen_channel(config_path) as (channel, metadata):
        simple_stage_service = StageServiceStub(channel=channel, metadata=metadata)

        # Lower the objective to Z=0 before moving laterally to prevent
        # collisions. The Z-drive is deliberately left at 0 afterwards; the
        # caller re-focuses (e.g. Definite Focus) at the new position.
        new_z = 0
        await MS_zenapi_focus.move_focus_to_new_z_position(new_z)

        posXY = await simple_stage_service.get_position(StageServiceGetPositionRequest())
        logger.info(
            f"Stage XY position before the move: "
            f"{posXY.x * 1e6:.2f} - {posXY.y * 1e6:.2f} [micron]"
        )

        # Perform the XY move (coordinates in metres).
        await simple_stage_service.move_to(StageServiceMoveToRequest(x=x, y=y))

        new_posXY = await simple_stage_service.get_position(StageServiceGetPositionRequest())
        logger.info(
            f"Stage XY position after the move: "
            f"{new_posXY.x * 1e6:.2f} - {new_posXY.y * 1e6:.2f} [micron]"
        )

        # Allow the stage to settle mechanically before the next operation.
        logger.info(f"Waiting for {STAGE_SETTLE_SECONDS} seconds...")
        await asyncio.sleep(STAGE_SETTLE_SECONDS)


async def set_stage_motion(speed_percent: Optional[float] = None,
                           acceleration_percent: Optional[float] = None,
                           ) -> Dict[str, Optional[float]]:
    """Set the XY-stage travel speed and acceleration (percent of max, [0, 100]).

    Speed/acceleration are device state, NOT per-move parameters: ``MoveTo`` has
    no velocity field, and the controller holds the values across SmartMic's
    per-call open/close of the gRPC channel. So this is the "global stage-speed
    control for a whole project" knob — set it once at the start of a run (see
    the shared ``preflight``) and leave it; every later move inherits it. No
    restore (restoring would defeat the purpose).

    Both are requested TOGETHER on purpose: for a live sample it is the
    acceleration (the jerk at the start/stop of a move) that sloshes the medium,
    so a low top speed paired with a high acceleration would still jolt the
    sample. **But they are applied INDEPENDENTLY**: confirmed on this gateway
    (2026-07-14) that ``SetSpeed``/``GetSpeed`` are rejected outright
    (``FAILED_PRECONDITION: "This parameter is not supported by the device."``)
    while ``SetAcceleration``/``GetAcceleration`` work fine at any value. Since
    acceleration is the parameter that actually matters for protecting a live
    sample, a speed failure is logged and tolerated rather than raised — it must
    NOT prevent acceleration from being applied. An acceleration failure, by
    contrast, DOES raise: that is the one knob this function exists to guarantee.

    Args:
        speed_percent: Target XY travel speed, percent of max. ``None`` falls
            back to ``STAGE_TRAVEL_SPEED_PERCENT`` (100 = full speed).
        acceleration_percent: Target XY acceleration, percent of max. ``None``
            falls back to ``STAGE_ACCELERATION_PERCENT`` (100 = full).

    Returns:
        The read-back after setting, as
        ``{"speed_x", "speed_y", "acceleration_x", "acceleration_y"}`` (percent).
        ``speed_x``/``speed_y`` are ``None`` when the device rejected the speed
        knob (see above) — that is NOT a failure of this function.

    Raises:
        ValueError: If a percent is not a finite number in [0, 100] (bool is
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

        # Acceleration is the parameter this function guarantees — let a failure
        # here propagate to the caller.
        await stage_service.set_acceleration(
            StageServiceSetAccelerationRequest(acceleration_x=accel, acceleration_y=accel)
        )
        ac = await stage_service.get_acceleration(StageServiceGetAccelerationRequest())

        # Speed is a known, permanent limitation on this gateway/device, not a
        # transient failure — tolerate it so it can never block acceleration
        # (already applied above) from taking effect.
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

        # Read back what is actually in effect, so the run log records the real
        # values (insurance against a silent overwrite by ZEN / a tile scan).
        logger.info(
            f"Stage motion set: speed=({sp_x}, {sp_y})%  "
            f"acceleration=({ac.x}, {ac.y})%"
        )
        return {
            "speed_x": sp_x, "speed_y": sp_y,
            "acceleration_x": ac.x, "acceleration_y": ac.y,
        }
