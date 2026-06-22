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
Software Autofocus (SWAF) helpers for ZEN blue / ZEN core via gRPC.

This module wraps the ZEN gRPC ``ExperimentSwAutofocusService``
(lm.acquisition.v1) together with the ``ExperimentService``
(acquisition.v1beta) needed to load and clone experiments.

SWAF searches for the best focus position by acquiring a z-stack and
maximising a contrast metric, without requiring dedicated hardware
(unlike Definite Focus).

Public functions
----------------
run_software_autofocus_from_path -- Load a SWAF .czexp BY PATH and run it (portable)
run_software_autofocus           -- Load a SWAF experiment BY NAME and run it
main                             -- CLI demo: configure and run SWAF on a named experiment

Both run_* functions share :func:`_find_auto_focus_with_retry` for the actual
search/retry loop; they differ only in how they obtain a runnable experiment id
(load by path vs. load by name) — exactly mirroring the run_experiment_* family
in ``MS_zenapi_experiment_methods``.
"""

import asyncio
import sys
from pathlib import Path
import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
from MS_zenapi_helpers import set_logging, open_zen_channel
from grpclib import GRPCError

# Auto-generated gRPC stubs for experiment loading and cloning.
from zen_api.acquisition.v1beta import (
    ExperimentServiceStub,
    ExperimentServiceLoadRequest,
    ExperimentServiceCloneRequest,
    ExperimentServiceSaveRequest,
)

# Auto-generated gRPC stubs for SWAF parameter access and execution.
from zen_api.lm.acquisition.v1 import (
    ExperimentSwAutofocusServiceStub,
    ExperimentSwAutofocusServiceGetAutofocusParametersRequest,
    ExperimentSwAutofocusServiceSetAutofocusParametersRequest,
    ExperimentSwAutofocusServiceFindAutoFocusRequest,
    AutofocusMode,
    AutofocusContrastMeasure,
    AutofocusSampling,
)

# Auto-generated gRPC stubs for reading the Z-drive position after SWAF.
from zen_api.lm.hardware.v2 import (
    FocusServiceGetPositionRequest,
    FocusServiceStub,
)

# config.ini path — single-sourced from zeiss_paths (repo root), not recomputed.
from zeiss_paths import CONFIG_PATH as config_path

# Default experiment names used by the CLI demo (main function).
expname = "ZEN_API_SWAF"
expname_cloned = "ZEN_API_SWAF_cloned"

# Paths used only by the CLI demo — not referenced by run_software_autofocus.
image_folder = Path(r"f:\Zen_Output\temp")
exp_folder = Path(r"f:\Documents\Carl Zeiss\ZEN\Documents\Experiment Setups")


async def _find_auto_focus_with_retry(
    swaf_service,
    experiment_id: str,
    label: str,
    timeout: int,
    max_retries: int,
    logger,
) -> tuple:
    """Run SWAF FindAutoFocus on an already-loaded experiment id, with retries.

    Shared tail of both public run_* functions: they differ only in HOW the
    experiment id is obtained (load by path vs. by name); the search itself —
    including the linear back-off retry (3 s, 6 s, 9 s …) on a transient gRPC
    error — is identical and lives here.

    Args:
        swaf_service:  an ``ExperimentSwAutofocusServiceStub``.
        experiment_id: id of an already-loaded, runnable experiment.
        label:         human-readable name for log messages (path stem or name).
        timeout:       max seconds ZEN may spend on a single SWAF search.
        max_retries:   total attempts including the first (e.g. 2 → one retry).
        logger:        the logger to emit to.

    Returns:
        Tuple ``(focus_pos_um, attempts_used)``; *focus_pos_um* is ``None`` when
        all attempts fail.
    """
    for attempt in range(max_retries):
        if attempt > 0:
            logger.warning(
                f"*** SWAF RETRY {attempt}/{max_retries - 1} ({label}) ***"
            )

        try:
            swaf_response = await swaf_service.find_auto_focus(
                ExperimentSwAutofocusServiceFindAutoFocusRequest(
                    experiment_id=experiment_id,
                    timeout=timeout,
                )
            )
            focus_pos_um = swaf_response.focus_position
            logger.info(f"SWAF ({label}): focus_position={focus_pos_um:.3f} µm")
            return focus_pos_um, attempt + 1

        except GRPCError as e:
            logger.error(
                f"SWAF ({label}) attempt {attempt + 1}/{max_retries} "
                f"failed: {e.message}"
            )
            if attempt < max_retries - 1:
                # Linear back-off: 3 s, 6 s, 9 s … between retries.
                wait = 3.0 * (attempt + 1)
                logger.info(f"Waiting {wait:.0f} s before SWAF retry ...")
                await asyncio.sleep(wait)

    logger.error(f"SWAF ({label}) failed after {max_retries} attempt(s).")
    return None, max_retries


async def run_software_autofocus_from_path(
    czexp_path,
    timeout: int = 30,
    max_retries: int = 2,
) -> tuple:
    """Load a SWAF experiment BY PATH and run it; return the focus position in µm.

    The portable counterpart to :func:`run_software_autofocus`: instead of a
    name that must already exist in ZEN's experiment library, this takes the
    full path of a ``.czexp`` file (e.g. one bundled with the repo / tests), so
    the call is self-contained and reproducible on any machine.

    Like :func:`MS_zenapi_experiment_methods.run_experiment_from_path`, it relies
    on ZEN's ``ExperimentService.Load`` accepting a FULL PATH (without the
    ``.czexp`` extension) in the ``experiment_name`` field, which yields a
    runnable experiment.

    Args:
        czexp_path: Path to the SWAF ``.czexp`` file to load and run.
        timeout: Maximum time in seconds ZEN may spend on a single SWAF search.
        max_retries: Total number of attempts including the first try.

    Returns:
        Tuple ``(focus_pos_um, attempts_used)``; *focus_pos_um* is ``None`` if
        all attempts failed.

    Raises:
        FileNotFoundError: if *czexp_path* does not exist.
    """
    logger = set_logging()

    czexp_path = Path(czexp_path)
    if not czexp_path.exists():
        raise FileNotFoundError(f"SWAF experiment file not found: {czexp_path}")

    async with open_zen_channel(config_path) as (channel, metadata):
        exp_service = ExperimentServiceStub(channel=channel, metadata=metadata)
        swaf_service = ExperimentSwAutofocusServiceStub(channel=channel, metadata=metadata)

        # Load BY PATH (without the .czexp extension) → runnable experiment.
        load_arg = str(czexp_path.with_suffix(""))
        logger.info("Loading SWAF experiment from path: " + load_arg)
        my_exp = await exp_service.load(
            ExperimentServiceLoadRequest(experiment_name=load_arg)
        )

        return await _find_auto_focus_with_retry(
            swaf_service, my_exp.experiment_id, czexp_path.stem,
            timeout, max_retries, logger,
        )


async def run_software_autofocus(
    experiment_name: str,
    timeout: int = 30,
    max_retries: int = 2,
) -> tuple:
    """Run SWAF using the named experiment and return the focus position in µm.

    Opens its own gRPC channel (same pattern as the other MS_zenapi_* modules)
    so it can be called as a one-shot operation without sharing channel state.

    On failure the search waits with a linear back-off (3 s, 6 s …) before each
    retry, giving ZEN time to recover from a transient gRPC error.

    Note: this loads the experiment BY NAME, so it must already exist in ZEN's
    experiment library.  For a self-contained / portable call (e.g. in tests),
    prefer :func:`run_software_autofocus_from_path`.

    Args:
        experiment_name: Name of the SWAF experiment to load (without the
            ``.czexp`` extension).
        timeout: Maximum time in seconds that ZEN is allowed to spend on a
            single SWAF search (default 30 s).
        max_retries: Total number of attempts including the first try
            (default 2, i.e. one retry on failure).

    Returns:
        Tuple ``(focus_pos_um, attempts_used)`` where *focus_pos_um* is the
        focus position in µm returned by ZEN, or ``None`` if all attempts
        failed.  *attempts_used* is the number of attempts made.
    """
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        exp_service = ExperimentServiceStub(channel=channel, metadata=metadata)
        swaf_service = ExperimentSwAutofocusServiceStub(channel=channel, metadata=metadata)

        my_exp = await exp_service.load(
            ExperimentServiceLoadRequest(experiment_name=experiment_name)
        )

        return await _find_auto_focus_with_retry(
            swaf_service, my_exp.experiment_id, experiment_name,
            timeout, max_retries, logger,
        )


def _show_swaf_info(logger, info) -> None:
    """Log the SWAF parameters of an experiment (CLI-demo helper).

    Vendored from ``zen_api_utils.experiment.show_swaf_info_LM`` so the demo
    ``main`` no longer depends on ``zen_api_utils``.  Limits in metres are
    logged in µm.

    Args:
        logger: the logger to emit to.
        info:   an ``ExperimentSwAutofocusServiceGetAutofocusParametersResponse``.
    """
    logger.info("------------  SWAF Information Start  ------------")
    logger.info(f"Mode: {info.auto_focus_mode}")
    logger.info(f"Sampling: {info.autofocus_sampling}")
    logger.info(f"Contrast Measure: {info.contrast_measure}")
    logger.info(f"Search Strategy: {info.search_strategy}")
    logger.info(f"Lower Limit: {info.lower_limit * 1e6:.3f}")
    logger.info(f"Upper Limit: {info.upper_limit * 1e6:.3f}")
    logger.info(f"Offset: {info.offset * 1e6:.3f}")
    logger.info(f"Reference Channel: {info.reference_channel_name}")
    logger.info(f"Relative Range Auto: {info.relative_range_is_automatic}")
    logger.info(f"Relative Search Range: {info.relative_search_range * 1e6:.3f}")
    logger.info(f"Acquisition ROI: {info.use_acquisition_roi}")
    logger.info("------------  SWAF Information End  ------------")


async def main(args):
    """CLI demo: configure SWAF parameters on a cloned experiment, then run it.

    Loads the experiment named :data:`expname`, inspects and modifies the SWAF
    parameters on a clone, saves the clone, re-inspects the parameters to
    verify the change, and finally runs SWAF and logs the resulting Z position.

    This function is intended as a development/demonstration tool and is not
    called by the production pipeline.

    Args:
        args: ``sys.argv`` (unused; present for consistency with other modules).

    Returns:
        None
    """
    # Set up the logger here so main() is self-contained (works even if it is
    # imported and called directly, not only via the __main__ guard below).
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        exp_service = ExperimentServiceStub(channel=channel, metadata=metadata)
        swaf_service = ExperimentSwAutofocusServiceStub(channel=channel, metadata=metadata)
        focus_service = FocusServiceStub(channel=channel, metadata=metadata)

        my_exp = await exp_service.load(ExperimentServiceLoadRequest(experiment_name=expname))

        # Read and display current SWAF settings before any modification.
        swaf_info = await swaf_service.get_autofocus_parameters(
            ExperimentSwAutofocusServiceGetAutofocusParametersRequest(
                experiment_id=my_exp.experiment_id
            )
        )
        _show_swaf_info(logger, swaf_info)

        logger.info("Cloning Experiment ...")
        my_exp_cloned = await exp_service.clone(
            ExperimentServiceCloneRequest(experiment_id=my_exp.experiment_id)
        )

        # Update the SWAF parameters on the clone.
        # Commented-out keyword args are shown here as a reference for available
        # options; they cannot be combined with relative_search_range when
        # relative_range_is_automatic is True (the default).
        await swaf_service.set_autofocus_parameters(
            ExperimentSwAutofocusServiceSetAutofocusParametersRequest(
                experiment_id=my_exp_cloned.experiment_id,
                autofocus_mode=AutofocusMode.CONTRAST,
                contrast_measure=AutofocusContrastMeasure.LOW_SIGNAL,
                search_strategy="Full",
                autofocus_sampling=AutofocusSampling.MEDIUM,
                # offset=0.5 * 1e-6,          # Z offset in [m] from the found position
                use_acquisition_roi=True,      # use full frame, not the Focus Region ROI
                reference_channel_name="EGFP",
                # relative_range_is_automatic=False,  # set False to supply an explicit range
                relative_search_range=123 * 1e-6,     # search range in [m]; requires auto=True
                # lower_limit=-50 * 1e-6,    # absolute lower Z limit in [m] (auto must be False)
                # upper_limit=50 * 1e-6,     # absolute upper Z limit in [m] (auto must be False)
            )
        )

        # Persist the modified clone so it can be reused in future sessions.
        logger.info("Saving Experiment ...")
        await exp_service.save(
            ExperimentServiceSaveRequest(
                experiment_id=my_exp_cloned.experiment_id,
                experiment_name=expname_cloned,
                allow_override=True,
            )
        )

        # Verify the saved parameters by reading them back from the clone.
        swaf_info = await swaf_service.get_autofocus_parameters(
            ExperimentSwAutofocusServiceGetAutofocusParametersRequest(
                experiment_id=my_exp_cloned.experiment_id
            )
        )
        _show_swaf_info(logger, swaf_info)

        # Record the Z position before SWAF so we can report how much it moved.
        posZ_before = await focus_service.get_position(FocusServiceGetPositionRequest())
        logger.info(f"Z-Drive Position before SWAF [micron]: {posZ_before.value * 1e6:.3f}")

        # Run SWAF on the modified clone and handle a possible timeout/gRPC error.
        try:
            swaf_response = await swaf_service.find_auto_focus(
                ExperimentSwAutofocusServiceFindAutoFocusRequest(
                    experiment_id=my_exp_cloned.experiment_id,
                    timeout=12,  # seconds
                )
            )
            logger.info(f"Z-Drive Position after SWAF: {swaf_response.focus_position:.3f}")

        except GRPCError as e:
            logger.error(e.message)
            logger.info(
                f"Z-Drive Position after SWAF error: {posZ_before.value * 1e6:.3f}"
            )


if __name__ == "__main__":
    logger = set_logging()
    asyncio.run(main(sys.argv))
