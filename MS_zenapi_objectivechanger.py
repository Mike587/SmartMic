# -*- coding: utf-8 -*-

#################################################################
# Based on    : zenapi_objectivechanger.py
# Author      : SRh
# Institution : Carl Zeiss Microscopy GmbH
#
# Copyright(c) 2025 Carl Zeiss AG, Germany. All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

#################################################################
# File        : MS_zenapi_objectivechanger.py
# Modified by : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#
# Wraps the ZEN gRPC ObjectiveChangerService and OptovarService
# (lm.hardware.v2) to provide helpers for switching objectives and
# optovar magnification changers.
#################################################################

"""
Objective changer and optovar helpers for ZEN blue / ZEN core via gRPC.

This module wraps two ZEN gRPC hardware services:

* ``ObjectiveChangerService`` (zen_api.lm.hardware.v2) -- motorised revolving
  nosepiece / objective changer
* ``OptovarService``          (zen_api.lm.hardware.v2) -- magnification
  changer (optovar) placed in the beam path

Public functions
----------------
set_objective_set_optovar         -- Move both objective and optovar to new positions
get_current_objective_and_optovar -- Query the current position indices of both devices
get_active_objective              -- The inventory entry (name, NA, immersion type) of the active objective
get_immersion_objective_positions -- Changer positions that hold an immersion objective
is_immersion_objective            -- True unless the API says the objective is dry (AIR)
get_current_objective_and_optovar_names -- Same, resolved to human-readable names
list_objectives_and_optovars      -- Read-only (name, position) inventory of both devices
"""

from typing import Any, Callable, List, Tuple

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
from MS_zenapi_helpers import (
    set_logging,
    open_zen_channel,
    get_used_objective_positions,
    get_objective_by_position,
    get_used_optovar_positions,
    get_optovar_by_position,
)

# Auto-generated gRPC stubs for objective changer and optovar services.
from zen_api.lm.hardware.v2 import (
    ObjectiveChangerServiceStub,
    ObjectiveChangerServiceGetPositionRequest,
    ObjectiveChangerServiceMoveToRequest,
    ObjectiveChangerServiceGetObjectivesRequest,
    ObjectiveChangerImmersionTypes,
    OptovarServiceStub,
    OptovarServiceGetPositionRequest,
    OptovarServiceMoveToRequest,
    OptovarServiceGetOptovarsRequest,
)

# config.ini path — single-sourced from zeiss_paths (repo root), not recomputed.
from zeiss_paths import CONFIG_PATH as config_path


def _require(lookup_fn: Callable[[Any, int], Any],
             used_positions_fn: Callable[[Any], List[int]],
             inventory: Any, position: int, context: str, kind: str) -> Any:
    """Return ``lookup_fn(inventory, position)``, or raise a clear error if unknown.

    lookup_fn returns None for a position not present in the reported
    inventory; dereferencing that None would otherwise surface as a generic
    AttributeError, which a caller's broad except/retry (see
    MS_CD7_API_LoA._change_optics) would then mask as just
    another transient failure instead of the real config/hardware mismatch.
    """
    item = lookup_fn(inventory, position)
    if item is None:
        raise ValueError(
            f"{context}: {kind} position {position} is not in the reported "
            f"{kind}s inventory (known positions: {used_positions_fn(inventory)})"
        )
    return item


def _require_objective(objectives: Any, position: int, context: str) -> Any:
    """Return the objective at *position*, or raise a clear error if unknown."""
    return _require(get_objective_by_position, get_used_objective_positions,
                     objectives, position, context, "objective")


def _require_optovar(optovars: Any, position: int, context: str) -> Any:
    """Return the optovar at *position*, or raise a clear error if unknown."""
    return _require(get_optovar_by_position, get_used_optovar_positions,
                     optovars, position, context, "optovar")


def _services(channel: Any, metadata: Any,
              ) -> Tuple[ObjectiveChangerServiceStub, OptovarServiceStub]:
    """The (objective changer, optovar) service stubs on one open channel."""
    return (ObjectiveChangerServiceStub(channel=channel, metadata=metadata),
            OptovarServiceStub(channel=channel, metadata=metadata))


async def _read_inventory(objchanger_service: ObjectiveChangerServiceStub,
                          optovar_service: OptovarServiceStub) -> Tuple[Any, Any]:
    """The full (objectives, optovars) inventory responses."""
    objectives = await objchanger_service.get_objectives(
        ObjectiveChangerServiceGetObjectivesRequest()
    )
    optovars = await optovar_service.get_optovars(OptovarServiceGetOptovarsRequest())
    return objectives, optovars


async def _read_positions(objchanger_service: ObjectiveChangerServiceStub,
                          optovar_service: OptovarServiceStub) -> Tuple[int, int]:
    """The current (objective, optovar) position indices."""
    pos_obj = await objchanger_service.get_position(ObjectiveChangerServiceGetPositionRequest())
    pos_optovar = await optovar_service.get_position(OptovarServiceGetPositionRequest())
    return pos_obj.value, pos_optovar.value


def is_immersion_objective(objective: Any) -> bool:
    """True unless the API says the objective is a dry objective.

    ``objective`` is an inventory entry (``ObjectiveData``). Only
    ``immersion_type == AIR`` counts as dry: WATER, OIL, GLYCERINE, SILICONE and
    also UNSPECIFIED count as immersion, so an objective that ZEN does not
    describe is treated the safe way (the Z=0 lower is refused).
    On this CD7 (read 2026-11-10): 5x, 20x/0.7, 20x/0.95 = AIR, 50x/1.2 = WATER.
    """
    return objective.immersion_type != ObjectiveChangerImmersionTypes.AIR


async def get_active_objective() -> Any:
    """Return the inventory entry of the objective that is in the beam path.

    Returns:
        The ``ObjectiveData`` of the active objective: ``name``, ``position``,
        ``na``, ``magnification``, ``immersion_type``.

    Raises:
        ValueError: The changer reports a position that is not in its inventory.
    """
    async with open_zen_channel(config_path) as (channel, metadata):
        objchanger_service, _ = _services(channel, metadata)
        objectives = await objchanger_service.get_objectives(
            ObjectiveChangerServiceGetObjectivesRequest()
        )
        pos_obj = await objchanger_service.get_position(ObjectiveChangerServiceGetPositionRequest())
        return _require_objective(objectives, pos_obj.value, "Current objective")


async def get_immersion_objective_positions() -> List[int]:
    """Return the changer positions that hold an immersion objective (sorted).

    Read from the inventory (``is_immersion_objective``), not from a constant.
    """
    async with open_zen_channel(config_path) as (channel, metadata):
        objchanger_service, _ = _services(channel, metadata)
        objectives = await objchanger_service.get_objectives(
            ObjectiveChangerServiceGetObjectivesRequest()
        )
        return sorted(o.position for o in objectives.objectives if is_immersion_objective(o))


async def set_objective_set_optovar(obj_new_position: int, opt_new_position: int) -> None:
    """Move the objective changer and optovar to the requested positions.

    Logs all available objectives and optovars, the initial positions, and the
    positions after each move so that changes are fully traceable in the run log.

    Args:
        obj_new_position: 1-based position index of the target objective on
            the revolving nosepiece.
        opt_new_position: 1-based position index of the target optovar
            setting.
    """
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        objchanger_service, optovar_service = _services(channel, metadata)

        # Fetch the full hardware inventory so we can resolve names from positions.
        objectives, optovars = await _read_inventory(objchanger_service, optovar_service)

        logger.info("-------- Available objectives and Optovars  ----------------------------")

        used_obj_positions = get_used_objective_positions(objectives)
        used_opt_positions = get_used_optovar_positions(optovars)
        logger.info(f"Used objective positions: {used_obj_positions}")
        logger.info(f"Used optovar positions: {used_opt_positions}")

        for obj in objectives.objectives:
            logger.info(f"Objective: {obj.name} - Position: {obj.position}")

        for opt in optovars.optovars:
            logger.info(f"Optovar: {opt.name} - Position: {opt.position}")

        logger.info("------------------ Move Objectives -----------------------")

        # Record where we started so the change is explicit in the log.
        pos_obj = await objchanger_service.get_position(ObjectiveChangerServiceGetPositionRequest())
        current_objective = _require_objective(objectives, pos_obj.value, "Current objective")
        logger.info(
            f"Current Objective: {current_objective.name} Position: {current_objective.position}"
        )

        await objchanger_service.move_to(
            ObjectiveChangerServiceMoveToRequest(position_index=obj_new_position)
        )
        requested_objective = _require_objective(objectives, obj_new_position, "New objective")
        logger.info(
            f"Requested Objective: {requested_objective.name} "
            f"Position: {requested_objective.position} (not a read-back)"
        )

        logger.info("------------------ Move Optovars -----------------------")

        pos_optovar = await optovar_service.get_position(OptovarServiceGetPositionRequest())
        current_optovar = _require_optovar(optovars, pos_optovar.value, "Current optovar")
        logger.info(
            f"Current Optovar: {current_optovar.name} Position: {current_optovar.position}"
        )

        await optovar_service.move_to(
            OptovarServiceMoveToRequest(position_index=opt_new_position)
        )
        requested_optovar = _require_optovar(optovars, opt_new_position, "New optovar")
        logger.info(
            f"Requested Optovar: {requested_optovar.name} "
            f"Position: {requested_optovar.position} (not a read-back)"
        )


async def get_current_objective_and_optovar() -> Tuple[int, int]:
    """Return the current position indices of the objective changer and optovar.

    Two RPCs only (the two positions): this is called by every move guard and
    in the poll loop of an objective change.

    Returns:
        Tuple ``(obj_position, opt_position)`` — both are integer position
        indices as reported by the hardware.
    """
    async with open_zen_channel(config_path) as (channel, metadata):
        return await _read_positions(*_services(channel, metadata))


async def list_objectives_and_optovars(
) -> Tuple[List[Tuple[str, int]], List[Tuple[str, int]]]:
    """Return the full objective and optovar inventory as ``(name, position)`` lists.

    Read-only — does not move any hardware.  Useful for discovering which
    position index corresponds to which magnification (e.g. the 5x objective
    or the 1x optovar).

    Returns:
        Tuple ``(objectives, optovars)`` where each is a list of
        ``(name, position)`` tuples.
    """
    async with open_zen_channel(config_path) as (channel, metadata):
        objectives, optovars = await _read_inventory(*_services(channel, metadata))

    obj_list = [(o.name, o.position) for o in objectives.objectives]
    opt_list = [(o.name, o.position) for o in optovars.optovars]
    return obj_list, opt_list


async def get_current_objective_and_optovar_names(
) -> Tuple[Tuple[str, int], Tuple[str, int]]:
    """Return the current objective and optovar as ``(name, position)`` pairs.

    Unlike :func:`get_current_objective_and_optovar`, which returns only the
    hardware position indices, this resolves the human-readable names (e.g.
    ``Plan-Apochromat 20x/0.8`` / ``2x``) so callers can verify the optics by
    magnification/NA rather than by slot number.

    Returns:
        Tuple ``((obj_name, obj_position), (opt_name, opt_position))``.

    Raises:
        ValueError: A current position is not in the reported inventory.
    """
    async with open_zen_channel(config_path) as (channel, metadata):
        services = _services(channel, metadata)
        objectives, optovars = await _read_inventory(*services)
        obj_position, opt_position = await _read_positions(*services)

    current_objective = _require_objective(objectives, obj_position, "Current objective")
    current_optovar = _require_optovar(optovars, opt_position, "Current optovar")
    return (
        (current_objective.name, current_objective.position),
        (current_optovar.name, current_optovar.position),
    )
