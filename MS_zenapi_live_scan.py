# -*- coding: utf-8 -*-

#################################################################
# File        : MS_zenapi_live_scan.py
# Author      : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#
# Read-only probe of the ZEN gRPC LiveScanService (lm.live_scan.v1),
# the service behind loading / ejecting the sample-carrier tray.
#
# Copyright(c) 2026 ETH Zurich (ScopeM). All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

"""
Live-scan (tray load / eject) configuration probe for ZEN via gRPC.

``LiveScanService`` (zen_api.lm.live_scan.v1) has four RPCs:
``LoadTrayAndPrescan``, ``EjectTray``, ``GetConfiguration`` and
``SetConfiguration``. Load and eject take no arguments; what the prescan does
on load (carrier detection, bottom-thickness / material measurement, barcodes,
overview, calibration) is set through the configuration.

This module wraps ONLY ``GetConfiguration``. It is read-only and moves nothing.
Load, eject and SetConfiguration are deliberately not wrapped: none of them has
been tried on the CD7, and an eject while the immersion objective is in water is
an untested hardware risk (see DEV_NOTES.md / CLAUDE.md).

Run it directly on the scope PC to see what this gateway reports::

    pixi run -e smartmic python MS_zenapi_live_scan.py

Public functions
----------------
get_live_scan_configuration    -- Read the live-scan configuration as a dict.
format_live_scan_configuration -- One log line for it (used by preflight);
                                  flags a template / ZEN carrier mismatch.
"""

import asyncio

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
from MS_zenapi_helpers import set_logging, open_zen_channel

# Auto-generated gRPC stubs for the live-scan service.
from zen_api.lm.live_scan.v1 import (
    LiveScanServiceStub,
    LiveScanServiceGetConfigurationRequest,
)

# config.ini path — single-sourced from zeiss_paths (repo root), not recomputed.
from zeiss_paths import CONFIG_PATH as config_path

# Field names of LiveScanServiceConfiguration, in proto order. Kept explicit so
# callers get a plain dict with stable keys, as in get_sample_carrier_info.
CONFIG_FIELDS = (
    "sample_holder_template",
    "live_scan_detection",
    "measure_bottom_thickness",
    "determine_bottom_material",
    "create_carrier_overview",
    "read_barcodes",
    "use_left_barcode",
    "use_right_barcode",
    "automatic_live_scan_calibration",
    "refractive_index",
    "material",
)


async def get_live_scan_configuration() -> dict:
    """Return ZEN's live-scan (tray load / prescan) configuration. Read-only.

    Calls ``LiveScanService.GetConfiguration``. gRPC errors are NOT swallowed:
    if this gateway does not support the service (e.g. UNIMPLEMENTED, or the
    INTERNAL rejection seen for SetSpeed), the ``GRPCError`` propagates.

    Returns:
        dict with the keys in ``CONFIG_FIELDS``:
            sample_holder_template          (str):   Carrier/holder type template.
            live_scan_detection             (bool):  Automatic carrier detection.
            measure_bottom_thickness        (bool):  Measure bottom thickness.
            determine_bottom_material       (bool):  Determine bottom material.
            create_carrier_overview         (bool):  Create a carrier overview image.
            read_barcodes                   (bool):  Read barcodes.
            use_left_barcode                (bool):  Use the left barcode reader.
            use_right_barcode               (bool):  Use the right barcode reader.
            automatic_live_scan_calibration (bool):  Automatic carrier calibration.
            refractive_index                (float): Bottom-material refractive index.
            material                        (str):   Bottom material name.
    """
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        service = LiveScanServiceStub(channel=channel, metadata=metadata)
        response = await service.get_configuration(LiveScanServiceGetConfigurationRequest())

    config = response.config
    result = {name: getattr(config, name) for name in CONFIG_FIELDS}
    logger.info(f"Live-scan configuration: {result}")
    return result


# The boolean prescan steps, in proto order (everything in CONFIG_FIELDS except
# the template, refractive index and material).
PRESCAN_FLAGS = tuple(
    f for f in CONFIG_FIELDS
    if f not in ("sample_holder_template", "refractive_index", "material")
)

# Suffix ZEN puts on a carrier template file name (seen on the CD7 2026-11-09:
# template 'Multiwell 6 - 250um.czsht' while SampleCarrierService reported
# 'Multiwell 6 - 250um'; DEV_NOTES "Tray load / eject").
TEMPLATE_SUFFIX = ".czsht"


def format_live_scan_configuration(cfg: dict, carrier=None) -> str:
    """One log line for a ``get_live_scan_configuration()`` dict.

    Lists the prescan steps that are on and off. If ``carrier`` (the
    SampleCarrierService name) is given and the template, without its
    ``.czsht`` suffix, is a different name, the line ends with a MISMATCH note.
    Formatting only — never raises on a missing key.
    """
    template = cfg.get("sample_holder_template")
    on = [f for f in PRESCAN_FLAGS if cfg.get(f) is True]
    off = [f for f in PRESCAN_FLAGS if cfg.get(f) is False]
    line = (f"Live-scan (tray load) config: template '{template}', "
            f"material {cfg.get('material')}, RI {cfg.get('refractive_index')}  |  "
            f"prescan on: {', '.join(on) or 'none'}  |  off: {', '.join(off) or 'none'}")
    if carrier is not None and isinstance(template, str):
        name = template[:-len(TEMPLATE_SUFFIX)] if template.endswith(TEMPLATE_SUFFIX) else template
        if name != carrier:
            line += f"  |  MISMATCH: ZEN carrier is '{carrier}'"
    return line


def _main():
    """Print the configuration, or the gRPC status if the gateway refuses."""
    from grpclib.exceptions import GRPCError

    try:
        cfg = asyncio.run(get_live_scan_configuration())
    except GRPCError as e:
        print(f"LiveScanService.GetConfiguration failed: {e.status.name}: {e.message}")
        raise SystemExit(1)

    width = max(len(k) for k in cfg)
    for key, value in cfg.items():
        print(f"{key:<{width}}  {value!r}")


if __name__ == "__main__":
    _main()
