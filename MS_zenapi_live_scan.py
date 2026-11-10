# -*- coding: utf-8 -*-

#################################################################
# File        : MS_zenapi_live_scan.py
# Author      : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#
# Wraps the ZEN gRPC LiveScanService (lm.live_scan.v1): the tray load /
# eject RPCs and the read-only prescan configuration.
#
# Copyright(c) 2026 ETH Zurich (ScopeM). All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

"""
Live-scan helpers for ZEN via gRPC: tray load / eject and the prescan configuration.

``LiveScanService`` (zen_api.lm.live_scan.v1) has four RPCs:
``LoadTrayAndPrescan``, ``EjectTray``, ``GetConfiguration`` and
``SetConfiguration``. Load and eject take no arguments; what the prescan does
on load (carrier detection, bottom-thickness / material measurement, barcodes,
overview, calibration) is set through the configuration.

``eject_tray`` and ``load_tray_and_prescan`` are the RAW RPCs. They have no
guards. Use the ``MS_CD7_API_LoA`` wrappers instead: they refuse while an
experiment runs, change from the 50x to the 5x before an eject, and reset a
``DefiniteFocusTracker``. ZEN itself retracts Z before it moves the tray, and a
load changes the objective to the 5x (DEV_NOTES "Tray load / eject").
``SetConfiguration`` is not wrapped yet (TODO.md).

Run the module directly on the scope PC to print the configuration (read-only)::

    pixi run -e smartmic python MS_zenapi_live_scan.py

Public functions
----------------
get_live_scan_configuration    -- Read the live-scan configuration as a dict.
format_live_scan_configuration -- One log line for it (used by preflight);
                                  flags a template / ZEN carrier mismatch.
eject_tray                     -- Raw EjectTray RPC (moves the tray out).
load_tray_and_prescan          -- Raw LoadTrayAndPrescan RPC (moves the tray
                                  in, changes the objective, runs the prescan).
"""

import asyncio
import time

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
from grpclib.exceptions import GRPCError
from MS_metadata import strip_czsht
from MS_zenapi_helpers import set_logging, open_zen_channel
from zeiss_paths import CONFIG_PATH as config_path  # config.ini at the repo root

# Auto-generated gRPC stubs for the live-scan service.
from zen_api.lm.live_scan.v1 import (
    LiveScanServiceStub,
    LiveScanServiceGetConfigurationRequest,
    LiveScanServiceEjectTrayRequest,
    LiveScanServiceLoadTrayAndPrescanRequest,
)

# RPC timeouts (s). From the ZEN UI on 2026-11-09: eject approximately 11 s,
# load + prescan approximately 21 s with every prescan step off. Thickness
# measurement, barcodes or carrier detection make the load longer.
EJECT_TIMEOUT_S = 120.0
LOAD_TIMEOUT_S = 600.0

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

def format_live_scan_configuration(cfg: dict, carrier=None) -> str:
    """One log line for a ``get_live_scan_configuration()`` dict.

    Lists the prescan steps that are on and off. If ``carrier`` (the
    SampleCarrierService name) is given and the template, without its
    ``.czsht`` suffix, is a different name, the line ends with a MISMATCH note.
    Formatting only — never raises on a missing key.

    The suffix: seen on the CD7 2026-11-09, template 'Multiwell 6 - 250um.czsht'
    while SampleCarrierService reported 'Multiwell 6 - 250um' (DEV_NOTES "Tray
    load / eject").
    """
    template = cfg.get("sample_holder_template")
    on = [f for f in PRESCAN_FLAGS if cfg.get(f) is True]
    off = [f for f in PRESCAN_FLAGS if cfg.get(f) is False]
    line = (f"Live-scan (tray load) config: template '{template}', "
            f"material {cfg.get('material')}, RI {cfg.get('refractive_index')}  |  "
            f"prescan on: {', '.join(on) or 'none'}  |  off: {', '.join(off) or 'none'}")
    if carrier is not None and isinstance(template, str):
        if strip_czsht(template) != carrier:
            line += f"  |  MISMATCH: ZEN carrier is '{carrier}'"
    return line


async def eject_tray(timeout_s: float = EJECT_TIMEOUT_S) -> list:
    """Raw ``LiveScanService.EjectTray``: move the tray out. NO guards.

    Use ``MS_CD7_API_LoA.eject_tray`` instead. Logs how long the RPC took (still
    to check: does it return at the end of the movement?).

    Returns:
        list[str]: The notifications ZEN returns.
    """
    return await _timed_rpc("eject_tray", LiveScanServiceEjectTrayRequest(), timeout_s)


async def load_tray_and_prescan(timeout_s: float = LOAD_TIMEOUT_S) -> list:
    """Raw ``LiveScanService.LoadTrayAndPrescan``: move the tray in and run the
    prescan with the current configuration. NO guards.

    ZEN changes the objective to the 5x during the load and does not change it
    back. Use ``MS_CD7_API_LoA.load_tray_and_prescan`` instead.

    Returns:
        list[str]: The notifications ZEN returns.
    """
    return await _timed_rpc("load_tray_and_prescan",
                            LiveScanServiceLoadTrayAndPrescanRequest(), timeout_s)


async def _timed_rpc(method: str, request, timeout_s: float) -> list:
    """Send one tray RPC (stub method name) with a timeout; log duration and notifications."""
    logger = set_logging()
    name = method
    logger.info(f"LiveScanService.{name}: sent (timeout {timeout_s:.0f} s).")
    t0 = time.monotonic()
    async with open_zen_channel(config_path) as (channel, metadata):
        service = LiveScanServiceStub(channel=channel, metadata=metadata)
        response = await getattr(service, method)(request, timeout=timeout_s)
    notifications = list(response.notifications)
    logger.info(f"LiveScanService.{name}: returned after {time.monotonic() - t0:.1f} s, "
                f"notifications: {notifications or 'none'}")
    return notifications


def _main():
    """Print the configuration, or the gRPC status if the gateway refuses."""
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
