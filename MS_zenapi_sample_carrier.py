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

"""Helper functions for the ``SampleCarrierService``: carrier data and containers.

A container is a sub-unit of a carrier. The ZEN user interface calls it a
"Scan Position". The container indices start at 0. The stage XY position is
relative to the selected container. On the CD7, ``GetCurrentContainer`` always
answers NOT_FOUND (DEV_NOTES "Multi-slide carriers").
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
    """Get the configured sample carrier from ``SampleCarrierService.GetInfo``.

    Returns:
        A dict with these keys: name, rows, columns, material, thickness,
        skirt, refractive_index. ``thickness`` is in µm, although the proto
        file says metres (DEV_NOTES "Dry objectives: DF finds the plate
        UNDERSIDE"). It is the measured value if ZEN measured it at the load.
        ``skirt`` uses the same scale. We expect µm, but this is not confirmed.
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
    """Get the container that the stage is in. This function is read-only.

    Returns:
        ``(row_index, column_index)``, or ``None`` if ZEN answers NOT_FOUND.
    """
    async with open_zen_channel(config_path) as (channel, metadata):
        service = SampleCarrierServiceStub(channel=channel, metadata=metadata)
        try:
            c = await service.get_current_container(
                SampleCarrierServiceGetCurrentContainerRequest())
        except GRPCError as e:
            if e.status == Status.NOT_FOUND:
                return None
            raise
    return c.row_index, c.column_index


async def move_to_container(row_index: int, column_index: int) -> None:
    """Lower Z to 0, then let ZEN move the stage to a container.

    The container indices start at 0. ZEN puts Z back to the value that it had
    at the call. That value is the height of the old slide, which can be above
    the focus on the new slide. Thus this function lowers Z to 0 first, and Z
    ends near 0. Focus again before you acquire (DEV_NOTES "Multi-slide
    carriers").

    Raises:
        MS_zenapi_stage_LM.ImmersionObjectiveActive (a RuntimeError): The
            immersion objective is active. A move of Z to 0 breaks the
            immersion bridge. The function moves nothing.
    """
    logger = set_logging()

    # The immersion guard must run BEFORE the move of Z to 0.
    # See move_stage_to_new_xy_position.
    await MS_zenapi_stage_LM.refuse_if_immersion_active("container move")

    # ZEN puts the CURRENT Z back after the switch. Thus lower Z first.
    await MS_zenapi_focus.move_focus_to_new_z_position(0)

    async with open_zen_channel(config_path) as (channel, metadata):
        service = SampleCarrierServiceStub(channel=channel, metadata=metadata)
        await service.move_to_container(SampleCarrierServiceMoveToContainerRequest(
            row_index=row_index, column_index=column_index))
    logger.info(f"Moved to sample-carrier container (row {row_index}, column {column_index}).")

    await asyncio.sleep(MS_zenapi_stage_LM.STAGE_SETTLE_SECONDS)
