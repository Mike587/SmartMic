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
# This module wraps the ZEN gRPC ObjectiveChangerService and OptovarService
# (lm.hardware.v2). It gives helper functions to change the objective and the
# optovar magnification changer.
#################################################################

"""Helper functions for the ``ObjectiveChangerService`` and the ``OptovarService``."""

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

from zeiss_paths import CONFIG_PATH as config_path


def _require(lookup_fn: Callable[[Any, int], Any],
             used_positions_fn: Callable[[Any], List[int]],
             inventory: Any, position: int, context: str, kind: str) -> Any:
    """Get ``lookup_fn(inventory, position)``.

    The function raises ``ValueError`` if the position is not known. A result
    of ``None`` becomes an ``AttributeError`` later, and a caller that tries
    again hides that error.
    """
    item = lookup_fn(inventory, position)
    if item is None:
        raise ValueError(
            f"{context}: {kind} position {position} is not in the reported "
            f"{kind}s inventory (known positions: {used_positions_fn(inventory)})"
        )
    return item


def _require_objective(objectives: Any, position: int, context: str) -> Any:
    """Get the objective at *position*, or raise a clear error if it is not known."""
    return _require(get_objective_by_position, get_used_objective_positions,
                    objectives, position, context, "objective")


def _require_optovar(optovars: Any, position: int, context: str) -> Any:
    """Get the optovar at *position*, or raise a clear error if it is not known."""
    return _require(get_optovar_by_position, get_used_optovar_positions,
                    optovars, position, context, "optovar")


def _services(channel: Any, metadata: Any,
              ) -> Tuple[ObjectiveChangerServiceStub, OptovarServiceStub]:
    """Get the objective-changer stub and the optovar stub on one open channel."""
    return (ObjectiveChangerServiceStub(channel=channel, metadata=metadata),
            OptovarServiceStub(channel=channel, metadata=metadata))


async def _read_inventory(objchanger_service: ObjectiveChangerServiceStub,
                          optovar_service: OptovarServiceStub) -> Tuple[Any, Any]:
    """Get the full inventory responses for the objectives and the optovars."""
    objectives = await objchanger_service.get_objectives(
        ObjectiveChangerServiceGetObjectivesRequest()
    )
    optovars = await optovar_service.get_optovars(OptovarServiceGetOptovarsRequest())
    return objectives, optovars


async def _read_positions(objchanger_service: ObjectiveChangerServiceStub,
                          optovar_service: OptovarServiceStub) -> Tuple[int, int]:
    """Get the current position indices of the objective and the optovar."""
    pos_obj = await objchanger_service.get_position(ObjectiveChangerServiceGetPositionRequest())
    pos_optovar = await optovar_service.get_position(OptovarServiceGetPositionRequest())
    return pos_obj.value, pos_optovar.value


def is_immersion_objective(objective: Any) -> bool:
    """Return ``True`` if the ``immersion_type`` of the ``ObjectiveData`` is not AIR.

    An ``immersion_type`` of UNSPECIFIED counts as immersion. Thus the code
    uses the safe condition for an objective that has no data, and it refuses
    the move of Z to 0. On this CD7, only the 50x/1.2 objective is WATER.
    """
    return objective.immersion_type != ObjectiveChangerImmersionTypes.AIR


async def get_active_objective() -> Any:
    """Get the ``ObjectiveData`` of the objective in the beam path.

    The data has these fields: name, position, na, magnification and
    immersion_type.

    Raises:
        ValueError: The changer reports a position that is not in its
            inventory.
    """
    async with open_zen_channel(config_path) as (channel, metadata):
        objchanger_service, _ = _services(channel, metadata)
        objectives = await objchanger_service.get_objectives(
            ObjectiveChangerServiceGetObjectivesRequest()
        )
        pos_obj = await objchanger_service.get_position(ObjectiveChangerServiceGetPositionRequest())
        return _require_objective(objectives, pos_obj.value, "Current objective")


async def get_immersion_objective_positions() -> List[int]:
    """Get the changer positions that the inventory marks as immersion, sorted."""
    async with open_zen_channel(config_path) as (channel, metadata):
        objchanger_service, _ = _services(channel, metadata)
        objectives = await objchanger_service.get_objectives(
            ObjectiveChangerServiceGetObjectivesRequest()
        )
        return sorted(o.position for o in objectives.objectives if is_immersion_objective(o))


async def set_objective_set_optovar(obj_new_position: int, opt_new_position: int) -> None:
    """Move the objective changer and the optovar to position indices that start at 1.

    This function does nothing for immersion. It writes the inventory and the
    positions to the log.

    Raises:
        ValueError: A current position or a requested position is not in the
            inventory.
    """
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        objchanger_service, optovar_service = _services(channel, metadata)

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
    """Get ``(obj_position, opt_position)``.

    The function sends only two RPCs, because each move guard and each poll
    loop calls it.
    """
    async with open_zen_channel(config_path) as (channel, metadata):
        return await _read_positions(*_services(channel, metadata))


async def get_current_objective_and_optovar_names(
) -> Tuple[Tuple[str, int], Tuple[str, int]]:
    """Get ``((obj_name, obj_position), (opt_name, opt_position))``.

    Raises:
        ValueError: A current position is not in the inventory that the
            hardware reports.
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
