# -*- coding: utf-8 -*-

#################################################################
# File        : MS_zenapi_sample_carrier.py
# Author      : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#
# Wraps the ZEN gRPC SampleCarrierService (lm.hardware.v1) to read
# information about the currently loaded sample carrier (well plate,
# slide, dish, ...).
#
# Copyright(c) 2026 ETH Zurich (ScopeM). All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

"""
Sample-carrier helpers for ZEN blue / ZEN core via gRPC.

This module wraps the ZEN gRPC ``SampleCarrierService`` (lm.hardware.v1).
The service exposes information about the sample carrier currently configured
in ZEN — its name, geometry (rows × columns) and bottom optical properties.

Public functions
----------------
get_sample_carrier_info  -- Read the full carrier description as a dict.
get_current_container    -- (row, column) of the container the stage is in, or
                            None if it is in none (ZEN: NOT_FOUND).
move_to_container        -- Lower Z, move the stage to a container (ZEN-driven
                            move), then wait for settling. Refuses with the
                            immersion objective active.

Containers are the sub-units of a carrier (e.g. the 3 slide positions of the
"Insert - 3x Slide" carrier, 1 row x 3 columns; indices start at 0). ZEN's UI
calls them "Scan Positions". On the CD7 (2026-09-29) stage XY is relative to
the selected container, MoveToContainer switches slides (ZEN retracts Z
during the switch and then puts the previous Z back — so move_to_container
lowers Z to 0 first), and GetCurrentContainer always answers NOT_FOUND — see
DEV_NOTES.md "Multi-slide carriers".
"""

import asyncio

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
from MS_zenapi_helpers import set_logging, open_zen_channel
import MS_zenapi_focus
import MS_zenapi_stage_LM

# Auto-generated gRPC stubs for the sample-carrier service.
from zen_api.lm.hardware.v1 import (
    SampleCarrierServiceStub,
    SampleCarrierServiceGetInfoRequest,
    SampleCarrierServiceGetCurrentContainerRequest,
    SampleCarrierServiceMoveToContainerRequest,
)

# config.ini path — single-sourced from zeiss_paths (repo root), not recomputed.
from zeiss_paths import CONFIG_PATH as config_path


async def get_sample_carrier_info() -> dict:
    """Return information about the currently configured sample carrier.

    Calls ``SampleCarrierService.GetInfo`` and returns the response as a
    plain dict so callers don't have to depend on the betterproto message
    type.

    Returns:
        dict with keys:
            name             (str):   The sample-carrier name.
            rows             (int):   Number of rows in the carrier.
            columns          (int):   Number of columns in the carrier.
            material         (str):   Bottom material of the carrier.
            thickness        (float): Bottom thickness. NOTE: the ZEN proto
                             annotates this as metres, but the device returns
                             micrometre-scale values (e.g. 195.0 for a 195 µm
                             plate bottom). Treat the unit as µm until verified.
            skirt            (float): Skirt / bottom offset, same unit caveat
                             as ``thickness`` (observed µm-scale, e.g. 2891.0).
            refractive_index (float): Refractive index of the bottom material.
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


async def get_current_container():
    """(row_index, column_index) of the container the stage is positioned in.

    Returns None when ZEN reports the stage is in no container (gRPC NOT_FOUND:
    "Stage is not positioned in any of the containers"). Read-only.
    """
    from grpclib.const import Status
    from grpclib.exceptions import GRPCError

    async with open_zen_channel(config_path) as (channel, metadata):
        service = SampleCarrierServiceStub(channel=channel, metadata=metadata)
        try:
            c = await service.get_current_container(SampleCarrierServiceGetCurrentContainerRequest())
        except GRPCError as e:
            if e.status == Status.NOT_FOUND:
                return None
            raise
    return c.row_index, c.column_index


async def move_to_container(row_index: int, column_index: int):
    """Move the stage to a container of the sample carrier (indices from 0).

    Same safety pattern as ``MS_zenapi_stage_LM.move_stage_to_new_xy_position``.
    The move itself is executed by ZEN (``SampleCarrierService.MoveToContainer``):
    it retracts Z (to ~−1000 µm), moves the stage, then puts back the Z it had
    when called (seen on the CD7, 2026-10-14). That restored Z is the old
    slide's height on the new slide, possibly past focus there. So Z is lowered
    to 0 first and ends near 0 (4.59 µm seen) on the new slide — re-focus
    before acquiring. With the immersion objective active the move is refused:
    ZEN's retract alone breaks the immersion bridge.

    Raises:
        MS_zenapi_stage_LM.ImmersionObjectiveActive (a RuntimeError): If the
            immersion objective is active (nothing is moved).
    """
    logger = set_logging()

    # Immersion guard — BEFORE the Z=0 lower (see move_stage_to_new_xy_position).
    await MS_zenapi_stage_LM.refuse_if_immersion_active("container move")

    # Lower the objective first: ZEN restores the CURRENT Z after the switch, so
    # this is what makes Z end near 0 on the new slide instead of at the old
    # slide's height.
    await MS_zenapi_focus.move_focus_to_new_z_position(0)

    async with open_zen_channel(config_path) as (channel, metadata):
        service = SampleCarrierServiceStub(channel=channel, metadata=metadata)
        await service.move_to_container(
            SampleCarrierServiceMoveToContainerRequest(row_index=row_index, column_index=column_index))
    logger.info(f"Moved to sample-carrier container (row {row_index}, column {column_index}).")

    # Same mechanical settle as after a direct XY move.
    await asyncio.sleep(MS_zenapi_stage_LM.STAGE_SETTLE_SECONDS)
