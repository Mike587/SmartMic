# -*- coding: utf-8 -*-

#################################################################
# Based on    : zenapi_definite_focus_find.py
# Author      : SRh
# Institution : Carl Zeiss Microscopy GmbH
#
# Copyright(c) 2025 Carl Zeiss AG, Germany. All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

#################################################################
# File        : MS_zenapi_focus.py
# Modified by : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#
# Wraps the ZEN gRPC FocusService (lm.hardware.v2) and the
# DefiniteFocusService (lm.acquisition.v1) to provide high-level
# helpers for Z-drive positioning and Definite Focus (DF) operations.
#
# Key functions
# -------------
# definite_focus_find_surface   -- Run DF FindSurface with retry logic
# definite_focus_recall         -- RecallFocus: back to the reflex + stored offset (20x/50x)
# move_focus_to_new_z_position  -- Move the Z-drive to an absolute position
# get_current_z_focus_position  -- Read the current Z-drive position
#################################################################

"""Z-drive (FocusService) and Definite Focus (DefiniteFocusService) helpers.

Each function opens and closes its own gRPC channel.
"""

import asyncio
from typing import Optional, Tuple

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
import MS_limits
from MS_zenapi_helpers import set_logging, open_zen_channel

from zen_api.lm.hardware.v2 import (
    FocusServiceGetPositionRequest,
    FocusServiceMoveToRequest,
    FocusServiceStub,
)

from zen_api.lm.acquisition.v1 import (
    DefiniteFocusServiceStub,
    DefiniteFocusServiceFindSurfaceRequest,
    DefiniteFocusServiceStoreFocusRequest,
    DefiniteFocusServiceRecallFocusRequest,
)

from zeiss_paths import CONFIG_PATH as config_path


# Retry policy of definite_focus_find_surface: after failed attempt n, wait
# n x DF_RETRY_BACKOFF_S, then start the next search n x DF_RETRY_NUDGE_M above
# start_z_m, so DF gets a slightly different starting point each time.
DF_RETRY_BACKOFF_S = 2.0
DF_RETRY_NUDGE_M = 50e-6


async def _read_z_um(focus_service: FocusServiceStub) -> float:
    """Current Z-drive position in µm."""
    pos = await focus_service.get_position(FocusServiceGetPositionRequest())
    return pos.value * 1e6


async def definite_focus_find_surface(max_retries: int = 3, start_z_m: float = -300e-6,
                                      store_focus: bool = False) -> Tuple[bool, int]:
    """Move Z to *start_z_m*, then run DF FindSurface with linear-backoff retries.

    A result z < 0 is a failure (the surface is always at positive Z here; DF
    found its own start). After failed attempt n: wait ``DF_RETRY_BACKOFF_S``
    x n, restart ``DF_RETRY_NUDGE_M`` x n above *start_z_m*.

    StoreFocus runs only on request, and never after a failed search (an offset
    stored at a random Z sends a later RecallFocus to a wrong Z). On 5x it is a
    full sweep that stores no offset (DEV_NOTES "What each DF RPC does").

    Args:
        max_retries: Maximum number of attempts, at least 1.
        start_z_m: Start Z in metres (default -300 µm); near the expected
            surface makes DF faster.
        store_focus: StoreFocus after success, for a later
            ``definite_focus_recall``.

    Returns:
        ``(True, attempts_used)``; failure raises.

    Raises:
        ValueError: If *max_retries* < 1.
        Exception: The last attempt's exception, with ``.attempts_used``
            attached (read by ``MS_CD7_API_LoA.df_find_surface``).
    """
    if max_retries < 1:
        raise ValueError(f"max_retries must be >= 1, got {max_retries}")
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        definite_focus_service = DefiniteFocusServiceStub(channel=channel, metadata=metadata)
        focus_service = FocusServiceStub(channel=channel, metadata=metadata)

        # Not reaching the start is non-fatal: -300 µm can be below the drive's
        # minimum ("Requested position was not reached"); DF searches from there.
        try:
            await focus_service.move_to(FocusServiceMoveToRequest(value=start_z_m))
        except Exception as move_err:
            logger.warning(
                f"Initial move to start Z {start_z_m * 1e6:.1f} µm not fully "
                f"reached ({move_err}); proceeding from the current Z."
            )
        logger.info(f"Initial Z-Position (ZDrive): {await _read_z_um(focus_service):.3f} [micron]")

        for attempt in range(1, max_retries + 1):
            if attempt > 1:
                logger.warning(f"*** DF FindSurface RETRY {attempt - 1}/{max_retries - 1} ***")

            try:
                found = await definite_focus_service.find_surface(
                    DefiniteFocusServiceFindSurfaceRequest()
                )
                fs_z_um = found.zposition * 1e6
                logger.info(f"Z-Position (FindSurface): {fs_z_um:.3f} [micron]")

                # z < 0: DF locked onto its own start, not the surface.
                if fs_z_um < 0:
                    raise RuntimeError(
                        f"FindSurface returned z={fs_z_um:.1f} µm — "
                        "likely failed to locate surface (expected positive Z)."
                    )

                z_um = await _read_z_um(focus_service)
                logger.info(f"Z-Position (ZDrive) after FS: {z_um:.3f} [micron]")

                if store_focus:
                    await definite_focus_service.store_focus(DefiniteFocusServiceStoreFocusRequest())
                    logger.info(f"Focus Position {z_um:.3f} [micron] stored.")
                return True, attempt

            except Exception as e:
                last_exception = e
                logger.error(
                    f"Definite Focus find_surface failed (attempt {attempt}/{max_retries}): {e}"
                )

            if attempt < max_retries:
                wait_s = DF_RETRY_BACKOFF_S * attempt
                logger.info(f"Waiting {wait_s:.1f} seconds before retry...")
                await asyncio.sleep(wait_s)

                retry_z_m = start_z_m + DF_RETRY_NUDGE_M * attempt
                try:
                    await focus_service.move_to(FocusServiceMoveToRequest(value=retry_z_m))
                    logger.info(f"Moved to Z: {retry_z_m * 1e6:.1f} microns before retry")
                except Exception as move_e:
                    logger.error(f"Could not move Z before retry: {move_e}")

        logger.error(f"Definite Focus find_surface failed after {max_retries} attempts")

        last_exception.attempts_used = max_retries
        raise last_exception


async def definite_focus_recall(max_retries: int = 3) -> Tuple[Optional[float], int]:
    """Go back to the reflex plus the stored offset (DF RecallFocus).

    Needs a previous StoreFocus (``definite_focus_find_surface(store_focus=True)``).
    Not a stored absolute Z. 5x ignores the offset; after an objective change it
    does a full FindSurface, on 50x without a seed (DEV_NOTES "What each DF RPC
    does", "50x: DF locks onto the plate UNDERSIDE").

    Returns:
        ``(z_um, attempts_used)``; *z_um* (ZDrive, µm) is ``None`` if all
        attempts failed (does not raise).
    """
    logger = set_logging()
    async with open_zen_channel(config_path) as (channel, metadata):
        definite_focus_service = DefiniteFocusServiceStub(channel=channel, metadata=metadata)
        focus_service = FocusServiceStub(channel=channel, metadata=metadata)

        last_exception = None
        for attempt in range(max_retries):
            try:
                z_recall = await definite_focus_service.recall_focus(
                    DefiniteFocusServiceRecallFocusRequest()
                )
                z_um = z_recall.zposition * 1e6
                logger.info(f"RecallFocus Z-Position: {z_um:.3f} [micron]")

                zpos = await focus_service.get_position(FocusServiceGetPositionRequest())
                logger.info(f"ZDrive after RecallFocus: {zpos.value * 1e6:.3f} [micron]")

                return zpos.value * 1e6, attempt + 1

            except Exception as e:
                last_exception = e
                logger.error(f"RecallFocus failed (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(DF_RETRY_BACKOFF_S * (attempt + 1))

        logger.error(f"RecallFocus failed after {max_retries} attempts")
        return None, max_retries


async def move_focus_to_new_z_position(z: float) -> None:
    """Move the Z-drive to *z* (metres, absolute). Z=0 breaks the immersion bridge.

    If a move to ~0 fails: accept a drive already within 10 µm of 0, else
    retry at 10 µm.

    Raises:
        ValueError: ``z`` not finite / outside ``MS_limits``. Nothing is moved.
        Exception: The original move error if the fallback also fails.
    """
    logger = set_logging()
    MS_limits.require_z_position(z)

    async with open_zen_channel(config_path) as (channel, metadata):
        focus_service = FocusServiceStub(channel=channel, metadata=metadata)

        try:
            await focus_service.move_to(FocusServiceMoveToRequest(value=z))
            zpos = await focus_service.get_position(FocusServiceGetPositionRequest())
            logger.info(f"Z-Position after move: {zpos.value * 1e6:.3f} [micron]")

        except Exception as e:
            logger.error(f"Error moving focus to z={z}: {e}")

            # Only the read-back is in this try (re-raises must stay outside).
            try:
                current_zpos = await focus_service.get_position(FocusServiceGetPositionRequest())
            except Exception as e2:
                logger.error(f"Could not get current position: {e2}")
                raise e
            logger.info(f"Current Z-Position: {current_zpos.value * 1e6:.3f} [micron]")

            if abs(z) >= 1e-6:
                raise e

            tolerance = 10e-6  # 10 µm tolerance
            if abs(current_zpos.value) < tolerance:
                logger.info(
                    f"Already close to z=0 ({current_zpos.value * 1e6:.3f} micron). "
                    "Continuing..."
                )
                return

            # Hardware may reject z=0 exactly; try 10 µm.
            logger.info("Trying to move to z=10e-6 instead...")
            try:
                await focus_service.move_to(FocusServiceMoveToRequest(value=10e-6))
                new_zpos = await focus_service.get_position(FocusServiceGetPositionRequest())
                logger.info(
                    f"Z-Position after alternative move: "
                    f"{new_zpos.value * 1e6:.3f} [micron]"
                )
                return
            except Exception as e3:
                logger.error(f"Alternative move also failed: {e3}")
                raise e


async def get_current_z_focus_position() -> float:
    """Return the current absolute Z-drive position in metres (not µm)."""
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        focus_service = FocusServiceStub(channel=channel, metadata=metadata)

        zpos = await focus_service.get_position(FocusServiceGetPositionRequest())
        logger.info(f"Z-Position (ZDrive): {zpos.value * 1e6:.3f} [micron]")

        return zpos.value
