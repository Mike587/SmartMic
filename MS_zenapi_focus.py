# -*- coding: utf-8 -*-

#################################################################
# File        : zenapi_definite_focus_find.py
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
# definite_focus_recall         -- Recall the last stored DF focus position
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
from pathlib import Path
import sys

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api / zen_api_utils resolve
from zen_api_utils.misc import set_logging, initialize_zenapi

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

# Resolve config.ini relative to this script so the module works regardless
# of the current working directory.
script_dir = Path(__file__).parent
config_path = script_dir / "config.ini"


async def definite_focus_find_surface(max_retries: int = 3, start_z_m: float = -300e-6):
    """Run Definite Focus FindSurface with exponential-backoff retry logic.

    Moves the Z-drive to *start_z_m* first so that DF always starts from a
    known position, which makes the surface search more reliable and faster.
    After a successful find the focus position is stored via ``store_focus``
    so that ``recall_focus`` can quickly return to it later.

    A result of z < 0 is treated as a failure because on this system the
    sample surface is always at a positive Z coordinate; a negative return
    value indicates that DF found its own starting position rather than the
    actual surface.

    On each retry the Z-drive is nudged upward by an additional 50 µm
    (50, 100, 150 … µm above *start_z_m*) to give DF a slightly different
    starting point, which can break out of a local failure mode.

    Args:
        max_retries: Maximum number of attempts (default 3).  The first
            attempt counts as attempt 1; retries are numbered 2 … max_retries.
        start_z_m:   Absolute Z position in metres to move to before starting
            the surface search (default −300 µm).  Pass a value close to the
            expected surface Z (e.g. last_surface_z − 100 µm) to speed up DF.

    Returns:
        Tuple ``(success, attempts_used)`` where *success* is ``True`` when
        FindSurface succeeded and the focus was stored, and *attempts_used*
        is the number of attempts that were made.

    Raises:
        Exception: Re-raises the last exception if all attempts fail.
    """
    logger = set_logging()

    channel, metadata = initialize_zenapi(config_path)
    definite_focus_service = DefiniteFocusServiceStub(channel=channel, metadata=metadata)
    focus_service = FocusServiceStub(channel=channel, metadata=metadata)

    # Move Z-drive to the requested starting position before the DF search.
    await focus_service.move_to(FocusServiceMoveToRequest(value=start_z_m))
    zpos = await focus_service.get_position(FocusServiceGetPositionRequest())
    logger.info(f"Initial Z-Position (ZDrive): {zpos.value * 1e6:.3f} [micron]")

    success = False
    last_exception = None
    attempts_used = 0

    for attempt in range(max_retries):
        attempts_used = attempt + 1

        if attempt > 0:
            logger.warning(f"*** DF FindSurface RETRY {attempt}/{max_retries - 1} ***")

        try:
            # Run DF FindSurface; the returned zposition is in metres.
            zpos_find_surface = await definite_focus_service.find_surface(
                DefiniteFocusServiceFindSurfaceRequest()
            )
            fs_z_um = zpos_find_surface.zposition * 1e6
            logger.info(f"Z-Position (FindSurface): {fs_z_um:.3f} [micron]")

            # Sanity-check: DF returning a negative Z means it locked onto its
            # own starting position rather than the actual sample surface.
            if fs_z_um < 0:
                raise RuntimeError(
                    f"FindSurface returned z={fs_z_um:.1f} µm — "
                    "likely failed to locate surface (expected positive Z)."
                )

            zpos = await focus_service.get_position(FocusServiceGetPositionRequest())
            logger.info(f"Z-Position (ZDrive) after FS: {zpos.value * 1e6:.3f} [micron]")

            # Persist the focus position so recall_focus can use it later.
            await definite_focus_service.store_focus(DefiniteFocusServiceStoreFocusRequest())
            logger.info(f"Focus Position {zpos.value * 1e6:.3f} [micron] stored.")

            success = True
            break

        except Exception as e:
            last_exception = e
            logger.error(
                f"Definite Focus find_surface failed (attempt {attempt + 1}/{max_retries}): {e}"
            )

            if attempt < max_retries - 1:
                # Exponential backoff: wait 2 s, 4 s, 6 s … between retries.
                wait_time = 2.0 * (attempt + 1)
                logger.info(f"Waiting {wait_time:.1f} seconds before retry...")
                await asyncio.sleep(wait_time)

                # Nudge Z upward (50, 100, 150 µm …) before the next attempt
                # to give DF a fresh starting point.
                try:
                    offset = (attempt + 1) * 50e-6  # metres
                    retry_z = start_z_m + offset
                    await focus_service.move_to(FocusServiceMoveToRequest(value=retry_z))
                    logger.info(f"Moved to Z: {retry_z * 1e6:.1f} microns before retry")
                except Exception as move_e:
                    logger.error(f"Could not move Z before retry: {move_e}")

    if not success:
        logger.error(f"Definite Focus find_surface failed after {max_retries} attempts")
        # Best-effort: store whatever the current Z is so that the pipeline
        # can at least attempt a recall_focus rather than being completely lost.
        try:
            current_zpos = await focus_service.get_position(FocusServiceGetPositionRequest())
            logger.info(
                f"Current Z-Position after all failed attempts: "
                f"{current_zpos.value * 1e6:.3f} [micron]"
            )
            try:
                await definite_focus_service.store_focus(DefiniteFocusServiceStoreFocusRequest())
                logger.info(
                    f"Stored current focus position {current_zpos.value * 1e6:.3f} [micron] "
                    "despite find_surface failure"
                )
            except Exception as e2:
                logger.error(f"Could not store focus position: {e2}")
        except Exception as e3:
            logger.error(f"Could not get current position after find_surface failure: {e3}")

        raise last_exception

    # Reference: move Z up by 500 µm then use recall_focus to return.
    # Kept here as a usage example; not executed during normal operation.
    '''
    await focus_service.move_to(FocusServiceMoveToRequest(value=zpos.value + 500 * 1e-6))
    new_posZ = await focus_service.get_position(FocusServiceGetPositionRequest())
    logger.info(f"New Z-Drive: {new_posZ.value * 1e6:.3f} [micron]")

    zpos_recall = await definite_focus_service.recall_focus(DefiniteFocusServiceRecallFocusRequest())
    logger.info(f"Z-Position (RecallFocus): {zpos_recall.zposition * 1e6:.3f} [micron]")
    '''

    channel.close()
    return success, attempts_used


async def definite_focus_recall(max_retries: int = 3):
    """Quickly return the Z-drive to the last stored Definite Focus position.

    Uses ``recall_focus`` rather than a full ``find_surface`` sweep, so it is
    much faster.  Requires that ``store_focus`` has been called previously
    (e.g. after a successful ``definite_focus_find_surface``).

    Args:
        max_retries: Maximum number of attempts (default 3).

    Returns:
        Tuple ``(z_um, attempts_used)`` where *z_um* is the ZDrive position
        in µm after a successful recall, or ``None`` if all attempts failed.
        *attempts_used* is the number of attempts that were made.
    """
    logger = set_logging()
    channel, metadata = initialize_zenapi(config_path)
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

            channel.close()
            return zpos.value * 1e6, attempt + 1

        except Exception as e:
            last_exception = e
            logger.error(f"RecallFocus failed (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                # Linear backoff: 2 s, 4 s, 6 s …
                await asyncio.sleep(2.0 * (attempt + 1))

    logger.error(f"RecallFocus failed after {max_retries} attempts")
    channel.close()
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

    channel, metadata = initialize_zenapi(config_path)
    definite_focus_service = DefiniteFocusServiceStub(channel=channel, metadata=metadata)
    focus_service = FocusServiceStub(channel=channel, metadata=metadata)

    try:
        await focus_service.move_to(FocusServiceMoveToRequest(value=z))
        zpos = await focus_service.get_position(FocusServiceGetPositionRequest())
        logger.info(f"Z-Position after move: {zpos.value * 1e6:.3f} [micron]")

    except Exception as e:
        logger.error(f"Error moving focus to z={z}: {e}")

        try:
            current_zpos = await focus_service.get_position(FocusServiceGetPositionRequest())
            logger.info(f"Current Z-Position: {current_zpos.value * 1e6:.3f} [micron]")

            if abs(z) < 1e-6:
                # Target is effectively zero — check if we are already close enough.
                tolerance = 10e-6  # 10 µm tolerance
                if abs(current_zpos.value) < tolerance:
                    logger.info(
                        f"Already close to z=0 ({current_zpos.value * 1e6:.3f} micron). "
                        "Continuing..."
                    )
                    channel.close()
                    return
                else:
                    # Hardware may not accept z=0 exactly; try 10 µm as a safe alternative.
                    logger.info("Trying to move to z=10e-6 instead...")
                    try:
                        await focus_service.move_to(FocusServiceMoveToRequest(value=10e-6))
                        new_zpos = await focus_service.get_position(
                            FocusServiceGetPositionRequest()
                        )
                        logger.info(
                            f"Z-Position after alternative move: "
                            f"{new_zpos.value * 1e6:.3f} [micron]"
                        )
                        channel.close()
                        return
                    except Exception as e3:
                        logger.error(f"Alternative move also failed: {e3}")
                        raise e
            else:
                raise e

        except Exception as e2:
            logger.error(f"Could not get current position: {e2}")
            raise e

    channel.close()


async def get_current_z_focus_position() -> float:
    """Return the current absolute Z-drive position in metres.

    Args:
        None

    Returns:
        Current Z position in metres (not µm).
    """
    logger = set_logging()

    channel, metadata = initialize_zenapi(config_path)
    definite_focus_service = DefiniteFocusServiceStub(channel=channel, metadata=metadata)
    focus_service = FocusServiceStub(channel=channel, metadata=metadata)

    zpos = await focus_service.get_position(FocusServiceGetPositionRequest())
    logger.info(f"Z-Position (ZDrive): {zpos.value * 1e6:.3f} [micron]")

    channel.close()
    return zpos.value


if __name__ == "__main__":
    logger = set_logging()
    asyncio.run(definite_focus_find_surface())
