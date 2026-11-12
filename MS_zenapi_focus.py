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
# This module wraps the ZEN gRPC FocusService (lm.hardware.v2) and the
# DefiniteFocusService (lm.acquisition.v1). It gives high-level helper
# functions for the Z-drive and for Definite Focus (DF).
#
# The primary functions
# ---------------------
# definite_focus_find_surface   Run DF FindSurface with retries.
# definite_focus_recall         Go back to the reflex plus the stored offset
#                               (20x and 50x).
# move_focus_to_new_z_position  Move the Z-drive to an absolute position.
# get_current_z_focus_position  Read the current Z-drive position.
#################################################################

"""Helper functions for the Z-drive and for Definite Focus.

The Z-drive functions use the ``FocusService``. The Definite Focus functions
use the ``DefiniteFocusService``. Each function opens and closes its own gRPC
channel.
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


# These two values control the retries of definite_focus_find_surface. After
# attempt n fails, the function waits n x DF_RETRY_BACKOFF_S. Then it starts
# the next search n x DF_RETRY_NUDGE_M above start_z_m. Thus DF gets a
# different start point for each attempt.
DF_RETRY_BACKOFF_S = 2.0
DF_RETRY_NUDGE_M = 50e-6


async def _read_z_um(focus_service: FocusServiceStub) -> float:
    """Get the current Z-drive position in µm."""
    pos = await focus_service.get_position(FocusServiceGetPositionRequest())
    return pos.value * 1e6


async def definite_focus_find_surface(max_retries: int = 3, start_z_m: float = -300e-6,
                                      store_focus: bool = False) -> Tuple[bool, int]:
    """Move Z to *start_z_m*, then run DF FindSurface with retries.

    A result of z < 0 is a failure. On this microscope the surface is always at
    a positive Z, thus DF found its own start point. After attempt n fails, the
    function waits ``DF_RETRY_BACKOFF_S`` x n. Then it starts again
    ``DF_RETRY_NUDGE_M`` x n above *start_z_m*.

    The function runs StoreFocus only if you ask for it. It never runs
    StoreFocus after a failed search, because an offset that it stores at an
    unknown Z sends a later RecallFocus to a wrong Z. On the 5x objective,
    StoreFocus does a full sweep and stores no offset (DEV_NOTES "What each DF
    RPC does").

    Args:
        max_retries: The maximum number of attempts. The minimum value is 1.
        start_z_m: The start Z in metres. The default is -300 µm. A start
            point near the expected surface makes DF faster.
        store_focus: Run StoreFocus after a success, for a later
            ``definite_focus_recall``.

    Returns:
        ``(True, attempts_used)``. A failure raises an exception.

    Raises:
        ValueError: *max_retries* is less than 1.
        Exception: The exception of the last attempt. The function attaches
            ``.attempts_used`` to it. ``MS_CD7_API_LoA.df_find_surface`` reads
            that attribute.
    """
    if max_retries < 1:
        raise ValueError(f"max_retries must be >= 1, got {max_retries}")
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        definite_focus_service = DefiniteFocusServiceStub(channel=channel, metadata=metadata)
        focus_service = FocusServiceStub(channel=channel, metadata=metadata)

        # A move that does not reach the start point is not an error. -300 µm
        # can be below the minimum of the drive ("Requested position was not
        # reached"). DF then searches from the position that it reached.
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

                # If z < 0, DF locked on its own start point, not on the surface.
                if fs_z_um < 0:
                    raise RuntimeError(
                        f"FindSurface returned z={fs_z_um:.1f} µm — "
                        "likely failed to locate surface (expected positive Z)."
                    )

                z_um = await _read_z_um(focus_service)
                logger.info(f"Z-Position (ZDrive) after FS: {z_um:.3f} [micron]")

                if store_focus:
                    await definite_focus_service.store_focus(
                        DefiniteFocusServiceStoreFocusRequest())
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
    """Go back to the reflex plus the stored offset with DF RecallFocus.

    This function needs a StoreFocus before it, from
    ``definite_focus_find_surface(store_focus=True)``. The stored value is an
    offset, not an absolute Z. The 5x objective ignores the offset. After an
    objective change, RecallFocus does a full FindSurface. On the 50x objective
    it does this with no start value (DEV_NOTES "What each DF RPC does" and
    "50x: DF locks onto the plate UNDERSIDE").

    Returns:
        ``(z_um, attempts_used)``. *z_um* is the Z-drive position in µm. It is
        ``None`` if all the attempts failed. The function raises no exception.
    """
    logger = set_logging()
    async with open_zen_channel(config_path) as (channel, metadata):
        definite_focus_service = DefiniteFocusServiceStub(channel=channel, metadata=metadata)
        focus_service = FocusServiceStub(channel=channel, metadata=metadata)

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
                logger.error(f"RecallFocus failed (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(DF_RETRY_BACKOFF_S * (attempt + 1))

        logger.error(f"RecallFocus failed after {max_retries} attempts")
        return None, max_retries


async def move_focus_to_new_z_position(z: float) -> None:
    """Move the Z-drive to the absolute position *z* in metres.

    WARNING: a Z of 0 breaks the immersion bridge.

    If a move to a Z near 0 fails, the function accepts a drive that is already
    within 10 µm of 0. If the drive is not that near, the function tries 10 µm.

    Raises:
        ValueError: ``z`` is not finite, or it is outside the range in
            ``MS_limits``. The function moves nothing.
        Exception: The error of the first move, if the second move also fails.
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

            # Only the read-back is in this try block. The raise statements
            # must stay outside it.
            try:
                current_zpos = await focus_service.get_position(FocusServiceGetPositionRequest())
            except Exception as e2:
                logger.error(f"Could not get current position: {e2}")
                raise e
            logger.info(f"Current Z-Position: {current_zpos.value * 1e6:.3f} [micron]")

            if abs(z) >= 1e-6:
                raise e

            tolerance = 10e-6  # 10 µm
            if abs(current_zpos.value) < tolerance:
                logger.info(
                    f"Already close to z=0 ({current_zpos.value * 1e6:.3f} micron). "
                    "Continuing..."
                )
                return

            # The hardware can refuse a Z of exactly 0. Try 10 µm.
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
    """Get the current absolute Z-drive position in metres, not in µm."""
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        focus_service = FocusServiceStub(channel=channel, metadata=metadata)

        zpos = await focus_service.get_position(FocusServiceGetPositionRequest())
        logger.info(f"Z-Position (ZDrive): {zpos.value * 1e6:.3f} [micron]")

        return zpos.value
