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

"""
Focus and Definite Focus helpers for ZEN blue / ZEN core via gRPC.

This module wraps two ZEN gRPC services:

* ``FocusService``         (zen_api.lm.hardware.v2)      -- raw Z-drive moves
* ``DefiniteFocusService`` (zen_api.lm.acquisition.v1)   -- hardware autofocus

Every public function opens its own gRPC channel, performs the requested
operation, closes the channel, and returns.  This keeps the call-site simple
and avoids shared-state issues when functions are called from a pipeline.
"""

import asyncio

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
from MS_zenapi_helpers import set_logging, open_zen_channel

# Auto-generated gRPC stubs for the Z-drive (FocusService)
from zen_api.lm.hardware.v2 import (
    FocusServiceGetPositionRequest,
    FocusServiceMoveToRequest,
    FocusServiceStub,
)

# Auto-generated gRPC stubs for Definite Focus (hardware autofocus)
from zen_api.lm.acquisition.v1 import (
    DefiniteFocusServiceStub,
    DefiniteFocusServiceFindSurfaceRequest,
    DefiniteFocusServiceStoreFocusRequest,
    DefiniteFocusServiceRecallFocusRequest,
)

# config.ini path — single-sourced from zeiss_paths (repo root), not recomputed.
from zeiss_paths import CONFIG_PATH as config_path


# Retry policy of definite_focus_find_surface: after failed attempt n, wait
# n x DF_RETRY_BACKOFF_S, then start the next search n x DF_RETRY_NUDGE_M above
# start_z_m, so DF gets a slightly different starting point each time.
DF_RETRY_BACKOFF_S = 2.0
DF_RETRY_NUDGE_M = 50e-6


async def _read_z_um(focus_service) -> float:
    """Current Z-drive position in µm."""
    pos = await focus_service.get_position(FocusServiceGetPositionRequest())
    return pos.value * 1e6


async def definite_focus_find_surface(max_retries: int = 3, start_z_m: float = -300e-6,
                                      store_focus: bool = False):
    """Run Definite Focus FindSurface with linear-backoff retry logic.

    Moves the Z-drive to *start_z_m* first so that DF always starts from a
    known position, which makes the surface search more reliable and faster.

    StoreFocus is optional (``store_focus``, default False). Measured on the
    CD7 on 2026-11-09 (DEV_NOTES "What each DF RPC does"): StoreFocus is the
    DF command ``InitOnCurrentPosition``. On the 20x and 50x objectives it
    records the offset to the reflex (0.45 to 0.9 s). On the 5x objective it
    does a full search sweep (+414 µm, 1.8 s) and records no offset. Set
    ``store_focus=True`` only if you use ``definite_focus_recall`` later.
    After a failed search nothing is stored: an offset recorded at a random Z
    would send a later RecallFocus to an incorrect Z.

    A result of z < 0 is treated as a failure because on this system the
    sample surface is always at a positive Z coordinate; a negative return
    value indicates that DF found its own starting position rather than the
    actual surface.

    After a failed attempt it waits ``DF_RETRY_BACKOFF_S`` x n and moves the
    Z-drive ``DF_RETRY_NUDGE_M`` x n above *start_z_m* (n = failed attempts so
    far) before trying again.

    Args:
        max_retries: Maximum number of attempts (default 3), at least 1.
        start_z_m:   Absolute Z position in metres to move to before starting
            the surface search (default −300 µm).  Pass a value close to the
            expected surface Z (e.g. last_surface_z − 100 µm) to speed up DF.
        store_focus: If True, do StoreFocus after a successful search, so that
            ``definite_focus_recall`` can return to this surface. Default False.

    Returns:
        Tuple ``(True, attempts_used)``.  It never returns ``(False, ...)``:
        failure raises.

    Raises:
        ValueError: If *max_retries* < 1.
        Exception: The last attempt's exception once all attempts have failed,
            with the real attempt count attached as ``.attempts_used`` (read by
            ``MS_CD7_API_LoA.run_definite_focus_find_surface`` for accurate
            retry telemetry).  Nothing is stored after a failure.
    """
    if max_retries < 1:
        raise ValueError(f"max_retries must be >= 1, got {max_retries}")
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        definite_focus_service = DefiniteFocusServiceStub(channel=channel, metadata=metadata)
        focus_service = FocusServiceStub(channel=channel, metadata=metadata)

        # The exact start does not need to be reached: DF FindSurface can search
        # from wherever the drive actually lands.  In particular the default
        # start (-300 µm) can be below the drive's reachable minimum on some
        # configurations, where ZEN reports "Requested position was not reached".
        # Treat that as non-fatal — land as close as the drive allows and let the
        # surface search proceed, rather than aborting the whole find.
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
                # The returned zposition is in metres.
                found = await definite_focus_service.find_surface(
                    DefiniteFocusServiceFindSurfaceRequest()
                )
                fs_z_um = found.zposition * 1e6
                logger.info(f"Z-Position (FindSurface): {fs_z_um:.3f} [micron]")

                # Sanity-check: DF returning a negative Z means it locked onto its
                # own starting position rather than the actual sample surface.
                if fs_z_um < 0:
                    raise RuntimeError(
                        f"FindSurface returned z={fs_z_um:.1f} µm — "
                        "likely failed to locate surface (expected positive Z)."
                    )

                z_um = await _read_z_um(focus_service)
                logger.info(f"Z-Position (ZDrive) after FS: {z_um:.3f} [micron]")

                if store_focus:
                    # Only for a later recall_focus (see the docstring).
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

        # Attach the real attempt count so a caller that only sees the exception
        # can still report accurate retry telemetry instead of guessing.
        last_exception.attempts_used = max_retries
        raise last_exception


async def definite_focus_recall(max_retries: int = 3):
    """Go back to the stored distance from the reflex (DF RecallFocus).

    Uses ``recall_focus`` rather than a full ``find_surface`` sweep, so it is
    much faster.  Requires that ``store_focus`` has been called previously
    (e.g. ``definite_focus_find_surface(store_focus=True)``).

    Measured on the CD7 (2026-11-09): on the 20x and 50x objectives RecallFocus
    goes back to the reflex plus the stored offset (±0.15 µm). On the 5x
    objective it goes to the reflex and does not use the offset. After an
    objective change it does a full FindSurface; on the 50x objective that is
    a search without a seed (DEV_NOTES "50x: DF locks onto the plate
    UNDERSIDE"). It does not return to a stored absolute Z.

    Args:
        max_retries: Maximum number of attempts (default 3).

    Returns:
        Tuple ``(z_um, attempts_used)`` where *z_um* is the ZDrive position
        in µm after a successful recall, or ``None`` if all attempts failed.
        *attempts_used* is the number of attempts that were made.
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
                    # Linear backoff: 2 s, 4 s, 6 s …
                    await asyncio.sleep(2.0 * (attempt + 1))

        logger.error(f"RecallFocus failed after {max_retries} attempts")
        return None, max_retries


async def move_focus_to_new_z_position(z: float):
    """Move the Z-drive to an absolute position.

    Handles the edge case where *z* is at or very near 0 m: some hardware
    configurations reject an exact move-to-zero command, so the function
    checks whether the drive is already within 10 µm of zero and, if not,
    retries with a small positive offset of 10 µm.

    Args:
        z: Target Z position in metres.

    Raises:
        Exception: Re-raises the original move exception if neither the
            primary move nor the fallback alternative succeeds.
    """
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        focus_service = FocusServiceStub(channel=channel, metadata=metadata)

        try:
            await focus_service.move_to(FocusServiceMoveToRequest(value=z))
            zpos = await focus_service.get_position(FocusServiceGetPositionRequest())
            logger.info(f"Z-Position after move: {zpos.value * 1e6:.3f} [micron]")

        except Exception as e:
            logger.error(f"Error moving focus to z={z}: {e}")

            # Only the read-back is guarded here. (The re-raises below used to sit
            # inside this try, so every failed move was also logged as "Could not
            # get current position: <the move error>" — misleading in run logs.)
            try:
                current_zpos = await focus_service.get_position(FocusServiceGetPositionRequest())
            except Exception as e2:
                logger.error(f"Could not get current position: {e2}")
                raise e
            logger.info(f"Current Z-Position: {current_zpos.value * 1e6:.3f} [micron]")

            if abs(z) >= 1e-6:
                raise e

            # Target is effectively zero — check if we are already close enough.
            tolerance = 10e-6  # 10 µm tolerance
            if abs(current_zpos.value) < tolerance:
                logger.info(
                    f"Already close to z=0 ({current_zpos.value * 1e6:.3f} micron). "
                    "Continuing..."
                )
                return

            # Hardware may not accept z=0 exactly; try 10 µm as a safe alternative.
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
    """Return the current absolute Z-drive position in metres.

    Args:
        None

    Returns:
        Current Z position in metres (not µm).
    """
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        focus_service = FocusServiceStub(channel=channel, metadata=metadata)

        zpos = await focus_service.get_position(FocusServiceGetPositionRequest())
        logger.info(f"Z-Position (ZDrive): {zpos.value * 1e6:.3f} [micron]")

        return zpos.value
