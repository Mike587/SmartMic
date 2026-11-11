# -*- coding: utf-8 -*-

#################################################################
# Based on    : zenapi_swaf.py
# Author      : SRh
# Institution : Carl Zeiss Microscopy GmbH
#
# Copyright(c) 2025 Carl Zeiss AG, Germany. All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

#################################################################
# File        : MS_zenapi_swaf.py
# Modified by : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#
# Wraps the ZEN gRPC ExperimentSwAutofocusService (lm.acquisition.v1)
# to provide helpers for Software Autofocus (SWAF) operations.
#################################################################

"""
Software autofocus (SWAF) over the ZEN gRPC ``ExperimentSwAutofocusService``.

SWAF acquires a short z-stack and picks the plane with the best contrast; no
DefiniteFocus hardware needed. The experiment is loaded BY PATH (like
``MS_zenapi_experiment_methods.run_experiment_from_path``). The synchronous,
raising entry point is ``MS_CD7_API_LoA.swaf_find_focus``.
"""

import asyncio
from pathlib import Path
from typing import Optional, Tuple, Union

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
from MS_zenapi_helpers import set_logging, open_zen_channel
from grpclib import GRPCError

from zen_api.acquisition.v1beta import (
    ExperimentServiceStub,
    ExperimentServiceLoadRequest,
)
from zen_api.lm.acquisition.v1 import (
    ExperimentSwAutofocusServiceStub,
    ExperimentSwAutofocusServiceFindAutoFocusRequest,
)

from zeiss_paths import CONFIG_PATH as config_path

# Wait before retry n (1-based): n x SWAF_RETRY_BACKOFF_S.
SWAF_RETRY_BACKOFF_S = 3.0


async def run_software_autofocus(
    czexp_path: Union[str, Path],
    timeout: int = 30,
    max_retries: int = 2,
) -> Tuple[Optional[float], int]:
    """Load a SWAF ``.czexp`` by path and run FindAutoFocus, with retries.

    Args:
        czexp_path: The SWAF ``.czexp`` file.
        timeout: Seconds ZEN may spend on one search.
        max_retries: Attempts including the first. A gRPC error is retried
            after 3 s, 6 s, … (``SWAF_RETRY_BACKOFF_S``).

    Returns:
        ``(focus_um, attempts)``; *focus_um* is None if every attempt failed.
        A focus of 0.0 µm is a result, not a failure.

    Raises:
        FileNotFoundError: *czexp_path* does not exist (before any RPC).
        ValueError: *max_retries* < 1.
    """
    if max_retries < 1:
        raise ValueError(f"max_retries must be >= 1, got {max_retries}")
    czexp_path = Path(czexp_path)
    if not czexp_path.exists():
        raise FileNotFoundError(f"SWAF experiment file not found: {czexp_path}")
    logger = set_logging()
    label = czexp_path.stem

    async with open_zen_channel(config_path) as (channel, metadata):
        exp_service = ExperimentServiceStub(channel=channel, metadata=metadata)
        swaf_service = ExperimentSwAutofocusServiceStub(channel=channel, metadata=metadata)

        load_arg = str(czexp_path.with_suffix(""))
        logger.info("Loading SWAF experiment from path: " + load_arg)
        loaded = await exp_service.load(ExperimentServiceLoadRequest(experiment_name=load_arg))

        for attempt in range(1, max_retries + 1):
            if attempt > 1:
                logger.warning(f"*** SWAF RETRY {attempt - 1}/{max_retries - 1} ({label}) ***")
            try:
                response = await swaf_service.find_auto_focus(
                    ExperimentSwAutofocusServiceFindAutoFocusRequest(
                        experiment_id=loaded.experiment_id, timeout=timeout))
                logger.info(f"SWAF ({label}): focus_position={response.focus_position:.3f} µm")
                return response.focus_position, attempt
            except GRPCError as e:
                logger.error(f"SWAF ({label}) attempt {attempt}/{max_retries} failed: {e.message}")
                if attempt < max_retries:
                    wait = SWAF_RETRY_BACKOFF_S * attempt
                    logger.info(f"Waiting {wait:.0f} s before SWAF retry ...")
                    await asyncio.sleep(wait)

    logger.error(f"SWAF ({label}) failed after {max_retries} attempt(s).")
    return None, max_retries
