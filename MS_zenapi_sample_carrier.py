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
"""

import asyncio
from pathlib import Path
import zeiss_paths  # noqa: F401  — extends sys.path so zen_api / zen_api_utils resolve
from zen_api_utils.misc import set_logging, initialize_zenapi

# Auto-generated gRPC stubs for the sample-carrier service.
from zen_api.lm.hardware.v1 import (
    SampleCarrierServiceStub,
    SampleCarrierServiceGetInfoRequest,
)

# Resolve config.ini relative to this script so the module works regardless
# of the current working directory.
script_dir = Path(__file__).parent
config_path = script_dir / "config.ini"


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

    channel, metadata = initialize_zenapi(config_path)
    service = SampleCarrierServiceStub(channel=channel, metadata=metadata)

    try:
        info = await service.get_info(SampleCarrierServiceGetInfoRequest())
    finally:
        channel.close()

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


if __name__ == "__main__":
    print(asyncio.run(get_sample_carrier_info()))
