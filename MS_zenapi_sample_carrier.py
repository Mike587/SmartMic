# -*- coding: utf-8 -*-

#################################################################
# File        : MS_zenapi_sample_carrier.py
# Author      : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#
# Copyright(c) 2026 ETH Zurich (ScopeM). All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

"""SampleCarrierService helpers: carrier info and containers.

Containers (ZEN UI: "Scan Positions") are the sub-units of a carrier, indices
from 0. Stage XY is relative to the selected container; GetCurrentContainer
answers NOT_FOUND on the CD7 (DEV_NOTES "Multi-slide carriers").
"""

import asyncio
from typing import Any, Dict, Optional, Tuple

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
from grpclib.const import Status
from grpclib.exceptions import GRPCError
from MS_zenapi_helpers import set_logging, open_zen_channel
import MS_zenapi_focus
import MS_zenapi_stage_LM

from zen_api.lm.hardware.v1 import (
    SampleCarrierServiceStub,
    SampleCarrierServiceGetInfoRequest,
    SampleCarrierServiceGetCurrentContainerRequest,
    SampleCarrierServiceMoveToContainerRequest,
)

from zeiss_paths import CONFIG_PATH as config_path


async def get_sample_carrier_info() -> Dict[str, Any]:
    """Return the configured sample carrier (``SampleCarrierService.GetInfo``) as a dict.

    Keys: name, rows, columns, material, thickness, skirt, refractive_index.
    ``thickness`` is in µm although the proto says metres (DEV_NOTES "Dry
    objectives: DF finds the plate UNDERSIDE"); the measured value if measured
    at load. ``skirt`` is on the same scale (µm expected, not confirmed).
    """
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        service = SampleCarrierServiceStub(channel=channel, metadata=metadata)
        info = await service.get_info(SampleCarrierServiceGetInfoRequest())

    logger.info(
        f"Sample carrier: '{info.name}'  "
        f"({info.rows}x{info.columns})  material={info.material}"
    )

    return {
        "name": info.name,
        "rows": info.rows,
        "columns": info.columns,
        "material": info.material,
        "thickness": info.thickness,
        "skirt": info.skirt,
        "refractive_index": info.refractive_index,
    }


async def get_current_container() -> Optional[Tuple[int, int]]:
    """(row_index, column_index) of the container the stage is in, or None (NOT_FOUND). Read-only."""
    async with open_zen_channel(config_path) as (channel, metadata):
        service = SampleCarrierServiceStub(channel=channel, metadata=metadata)
        try:
            c = await service.get_current_container(SampleCarrierServiceGetCurrentContainerRequest())
        except GRPCError as e:
            if e.status == Status.NOT_FOUND:
                return None
            raise
    return c.row_index, c.column_index


async def move_to_container(row_index: int, column_index: int) -> None:
    """Lower Z to 0, then let ZEN move the stage to a container (indices from 0).

    ZEN restores the Z it had when called (the old slide's height, possibly
    past focus on the new one), so Z is lowered to 0 first and ends near 0:
    re-focus before acquiring (DEV_NOTES "Multi-slide carriers").

    Raises:
        MS_zenapi_stage_LM.ImmersionObjectiveActive (a RuntimeError): The
            immersion objective is active; the Z=0 lower would break the
            immersion bridge. Nothing is moved.
    """
    logger = set_logging()

    # Immersion guard — BEFORE the Z=0 lower (see move_stage_to_new_xy_position).
    await MS_zenapi_stage_LM.refuse_if_immersion_active("container move")

    # ZEN restores the CURRENT Z after the switch: lower first.
    await MS_zenapi_focus.move_focus_to_new_z_position(0)

    async with open_zen_channel(config_path) as (channel, metadata):
        service = SampleCarrierServiceStub(channel=channel, metadata=metadata)
        await service.move_to_container(
            SampleCarrierServiceMoveToContainerRequest(row_index=row_index, column_index=column_index))
    logger.info(f"Moved to sample-carrier container (row {row_index}, column {column_index}).")

    await asyncio.sleep(MS_zenapi_stage_LM.STAGE_SETTLE_SECONDS)
