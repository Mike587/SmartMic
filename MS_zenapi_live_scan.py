# -*- coding: utf-8 -*-

#################################################################
# File        : MS_zenapi_live_scan.py
# Author      : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#
# Copyright(c) 2026 ETH Zurich (ScopeM). All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

"""Helper functions for the ``LiveScanService``: the tray and the prescan.

``eject_tray`` and ``load_tray_and_prescan`` are the RAW RPCs. They have NO
guards. Use the wrappers in ``MS_CD7_API_LoA``. The wrappers refuse to move
the tray while an experiment runs, change from the 50x to the 5x objective
before an eject, and reset a ``DefiniteFocusTracker``. ZEN moves Z back before
it moves the tray. A load changes the objective to the 5x (DEV_NOTES "Tray
load / eject"). This module does not wrap ``SetConfiguration``.

To print the configuration, which is read-only, use this command::

    pixi run -e smartmic python MS_zenapi_live_scan.py
"""

import asyncio
import time
from typing import Any, Dict, List, Optional

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
from grpclib.exceptions import GRPCError
from MS_metadata import strip_czsht
from MS_zenapi_helpers import set_logging, open_zen_channel
from zeiss_paths import CONFIG_PATH as config_path  # config.ini at the repo root

from zen_api.lm.live_scan.v1 import (
    LiveScanServiceStub,
    LiveScanServiceGetConfigurationRequest,
    LiveScanServiceEjectTrayRequest,
    LiveScanServiceLoadTrayAndPrescanRequest,
)

# The time limits of the tray RPCs, in seconds. The prescan steps make a load
# longer (DEV_NOTES "Tray load / eject").
EJECT_TIMEOUT_S = 120.0
LOAD_TIMEOUT_S = 600.0

# The fields of LiveScanServiceConfiguration, in the order of the proto file.
# They are the dict keys, and the order does not change.
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


async def get_live_scan_configuration() -> Dict[str, Any]:
    """Get the live-scan configuration of ZEN. This function is read-only.

    Returns:
        A dict whose keys are the names in ``CONFIG_FIELDS``.

    Raises:
        GRPCError: The gateway does not support this service. The function
            sends the error to the caller.
    """
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        service = LiveScanServiceStub(channel=channel, metadata=metadata)
        response = await service.get_configuration(LiveScanServiceGetConfigurationRequest())

    config = response.config
    result = {name: getattr(config, name) for name in CONFIG_FIELDS}
    logger.info(f"Live-scan configuration: {result}")
    return result


# The prescan steps that have a boolean value.
PRESCAN_FLAGS = tuple(
    f for f in CONFIG_FIELDS
    if f not in ("sample_holder_template", "refractive_index", "material")
)


def format_live_scan_configuration(cfg: Dict[str, Any],
                                   carrier: Optional[str] = None) -> str:
    """Make one log line from a ``get_live_scan_configuration()`` dict.

    The line shows which prescan steps are on and which are off. The function
    adds a MISMATCH note if *carrier*, which is the name from the
    ``SampleCarrierService``, is different from the template name without its
    ``.czsht`` suffix. A key that is not in the dict raises no exception.
    """
    template = cfg.get("sample_holder_template")
    on = [f for f in PRESCAN_FLAGS if cfg.get(f) is True]
    off = [f for f in PRESCAN_FLAGS if cfg.get(f) is False]
    line = (f"Live-scan (tray load) config: template '{template}', "
            f"material {cfg.get('material')}, RI {cfg.get('refractive_index')}  |  "
            f"prescan on: {', '.join(on) or 'none'}  |  off: {', '.join(off) or 'none'}")
    if (carrier is not None and isinstance(template, str)
            and strip_czsht(template) != carrier):
        line += f"  |  MISMATCH: ZEN carrier is '{carrier}'"
    return line


async def eject_tray(timeout_s: float = EJECT_TIMEOUT_S) -> List[str]:
    """Move the tray out with the raw ``LiveScanService.EjectTray``. NO guards.

    Use ``MS_CD7_API_LoA.eject_tray`` in place of this function.

    Returns:
        The notifications from ZEN.
    """
    return await _timed_rpc("eject_tray", LiveScanServiceEjectTrayRequest(), timeout_s)


async def load_tray_and_prescan(timeout_s: float = LOAD_TIMEOUT_S) -> List[str]:
    """Move the tray in, then run the prescan, with the raw RPC. NO guards.

    The RPC is ``LiveScanService.LoadTrayAndPrescan``. ZEN changes the
    objective to the 5x and does not change it back. Use
    ``MS_CD7_API_LoA.load_tray_and_prescan`` in place of this function.

    Returns:
        The notifications from ZEN.
    """
    return await _timed_rpc("load_tray_and_prescan",
                            LiveScanServiceLoadTrayAndPrescanRequest(), timeout_s)


async def _timed_rpc(method: str, request: Any, timeout_s: float) -> List[str]:
    """Send one tray RPC with a time limit.

    *method* is the name of the stub method. The function writes the duration
    and the notifications to the log.
    """
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


def _main() -> None:
    """Print the configuration. If the gateway refuses, print the gRPC status."""
    try:
        cfg = asyncio.run(get_live_scan_configuration())
    except GRPCError as e:
        print(f"LiveScanService.GetConfiguration failed: {e.status.name}: {e.message}")
        raise SystemExit(1) from e

    width = max(len(k) for k in cfg)
    for key, value in cfg.items():
        print(f"{key:<{width}}  {value!r}")


if __name__ == "__main__":
    _main()
