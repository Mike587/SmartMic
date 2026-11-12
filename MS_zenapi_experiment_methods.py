# -*- coding: utf-8 -*-

#################################################################
# Based on    : zenapi_experiment_methods.py
# Author      : SRh, JSm
# Institution : Carl Zeiss Microscopy GmbH
#
# Copyright(c) 2025 Carl Zeiss AG, Germany. All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

#################################################################
# File        : MS_zenapi_experiment_methods.py
# Modified by : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#
# This module wraps the ZEN gRPC ExperimentService (acquisition.v1beta). It
# runs a .czexp file by its path, collects the result image, and reads or
# stops the experiment that runs.
#################################################################

"""Helper functions for the ``ExperimentService``.

These functions run a ``.czexp`` file by its path, and read or stop the
experiment that is active. ZEN saves each result in its own default folder.
This module moves the result to the folder of the caller. Then it deletes each
other ``.czi`` file in the default folder of ZEN (DEV_NOTES "The cleanup
deletes everything in ZEN's default folder").
"""

import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, TypedDict, Union

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
from grpclib import GRPCError
from grpclib.const import Status

# The single source of the default output folder.
from MS_run_log import DEFAULT_EXPERIMENT_OUTPUT_FOLDER
from MS_zenapi_helpers import open_zen_channel, set_logging
from zeiss_paths import CONFIG_PATH as config_path  # config.ini at the repo root

from zen_api.acquisition.v1beta import (
    ExperimentServiceGetImageOutputPathRequest,
    ExperimentServiceGetStatusRequest,
    ExperimentServiceLoadRequest,
    ExperimentServiceRunExperimentRequest,
    ExperimentServiceStopRequest,
    ExperimentServiceStub,
)


class ExperimentResult(TypedDict):
    """The result of ``run_experiment_from_path``."""
    exp_result_path: Path          # The .czi file that ZEN acquired, in its final folder.
    experiment_id: str


# The id of the most recent experiment that this process started.
# stop_running_experiment uses it.
_last_started_experiment_id: Optional[str] = None


# ---------------------------------------------------------------------------
# The shared acquisition helper functions
# ---------------------------------------------------------------------------

async def _get_default_image_folder(exp_service: ExperimentServiceStub,
                                    logger: logging.Logger) -> Path:
    """Read the default image output folder of ZEN.

    This is read-only. The API has no function to set the folder. Set it in
    ZEN.
    """
    save_path = await exp_service.get_image_output_path(
        ExperimentServiceGetImageOutputPathRequest()
    )
    default_image_folder = Path(save_path.image_output_path)
    logger.info("Default Saving Location for CZI Images:" + str(default_image_folder))
    return default_image_folder


def _resolve_image_folder(custom_image_folder: Union[str, Path, None],
                          logger: logging.Logger) -> Path:
    """Find the target output folder and make it.

    The folder is the folder of the caller, or the default folder.
    """
    if custom_image_folder is not None:
        image_folder = Path(custom_image_folder)
    else:
        image_folder = DEFAULT_EXPERIMENT_OUTPUT_FOLDER
    image_folder.mkdir(parents=True, exist_ok=True)
    logger.info("Custom Saving Location for CZI Images:" + str(image_folder))
    return image_folder


def _same_folder(a: Path, b: Path) -> bool:
    """Return ``True`` if the two paths give the same folder.

    On Windows the comparison ignores the letter case and the form of the path.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:      # one of them does not exist
        return (os.path.normcase(os.path.abspath(a))
                == os.path.normcase(os.path.abspath(b)))


def _refuse_zen_default_folder(image_folder: Path, default_image_folder: Path) -> None:
    """Raise ``ValueError`` if the output folder is the default folder of ZEN.

    In that folder, the cleanup deletes all the earlier results (DEV_NOTES
    "The cleanup deletes everything in ZEN's default folder").
    """
    if _same_folder(image_folder, default_image_folder):
        raise ValueError(
            f"The output folder {image_folder} is ZEN's default image folder. The "
            f"cleanup after each acquisition deletes all other .czi files there, "
            f"so earlier results of this run would be lost. Give another folder "
            f"(custom_image_folder / custom_folder)."
        )


def _make_czi_basename(custom_filename: Optional[str]) -> str:
    """Make the base name of the output file, without the ``.czi`` suffix.

    The name is *custom_filename*, or a name that contains a UUID.
    """
    if custom_filename is not None:
        return custom_filename[:-4] if custom_filename.endswith(".czi") else custom_filename
    return f"zenapi_{str(uuid.uuid4())[:8]}"


def _unique_czi_name(image_folder: Path, czi_name: str, logger: logging.Logger) -> str:
    """Add a counter to the name, so the code never writes over a result file."""
    base_czi_name = czi_name
    counter = 1
    while (image_folder / (czi_name + ".czi")).exists():
        czi_name = f"{base_czi_name}_{counter:06d}"
        counter += 1
    if czi_name != base_czi_name:
        logger.info(f"File {base_czi_name}.czi already exists. Using {czi_name}.czi instead.")
    return czi_name


def _move_result_and_cleanup(exp_result: Any, default_image_folder: Path,
                             image_folder: Path, logger: logging.Logger) -> Path:
    """Move the acquired ``.czi`` file to *image_folder*.

    The function then deletes each other ``.czi`` file in the default folder.

    Returns:
        The final path of the result.
    """
    exp_default_path = default_image_folder / (exp_result.output_name + ".czi")
    exp_custom_path = image_folder / (exp_result.output_name + ".czi")
    # Resolve the paths before the comparison, so the code never renames a
    # file onto itself.
    if exp_default_path.resolve() != exp_custom_path.resolve() and exp_default_path.exists():
        # Use shutil.move, not rename. A move can cross drives (WinError 17).
        shutil.move(str(exp_default_path), str(exp_custom_path))
        logger.info(f"Moved experiment result from {exp_default_path} to {exp_custom_path}")
        result_path = exp_custom_path
    else:
        result_path = exp_default_path
    logger.info("Final Experiment Location: " + str(result_path))

    # Delete each .czi file that stays in the default folder of ZEN. NEVER
    # delete the result itself.
    result_resolved = result_path.resolve()
    temp_files_after = [
        f for f in default_image_folder.glob("*.czi")
        if f.resolve() != result_resolved
    ]
    for temp_file in temp_files_after:
        try:
            temp_file.unlink()
            logger.info(f"Cleaned up temp file: {temp_file}")
        except Exception as e:
            logger.warning(f"Could not delete temp file {temp_file}: {e}")
    logger.info(f"Cleaned up {len(temp_files_after)} files from default temp folder.")
    return result_path


async def _run_experiment_and_collect(
    exp_service: ExperimentServiceStub, experiment_id: str, default_image_folder: Path,
    image_folder: Path, czi_name: str, logger: logging.Logger,
) -> Path:
    """Run the experiment with the given id, under a name that is not in use.

    The function writes the status to the log, moves the result, and deletes
    the other files.

    Returns:
        The final path of the result.
    """
    global _last_started_experiment_id
    czi_name = _unique_czi_name(image_folder, czi_name, logger)
    _last_started_experiment_id = experiment_id
    logger.info("Starting Experiment Execution ...")
    logger.info(f"Using output name: {czi_name}")
    exp_result = await exp_service.run_experiment(
        ExperimentServiceRunExperimentRequest(
            experiment_id=experiment_id, output_name=czi_name
        )
    )
    exp_status = await exp_service.get_status(
        ExperimentServiceGetStatusRequest(experiment_id=experiment_id)
    )
    logger.info(exp_status)
    return _move_result_and_cleanup(exp_result, default_image_folder, image_folder, logger)


# ---------------------------------------------------------------------------
# The public functions
# ---------------------------------------------------------------------------

async def run_experiment_from_path(
    czexp_path: Union[str, Path],
    custom_image_folder: Union[str, Path, None] = None,
    custom_filename: Optional[str] = None,
) -> ExperimentResult:
    """Load a ``.czexp`` file by its path, run it, and move the result.

    ``ExperimentService.Load`` needs a full path WITHOUT the ``.czexp``
    suffix. Do not use Import and then run. ZEN cannot run an experiment with
    tiles or regions that you import, and it raises a NullReferenceException
    at the start.

    Args:
        czexp_path: The ``.czexp`` file.
        custom_image_folder: The folder that the function moves the ``.czi``
            file to. ``None`` gives ``DEFAULT_EXPERIMENT_OUTPUT_FOLDER``. The
            function refuses the default folder of ZEN
            (``_refuse_zen_default_folder``).
        custom_filename: The base name of the output file. ``None`` gives
            ``zenapi_<8 hex characters>``. A file that exists gets a counter.

    Raises:
        FileNotFoundError: *czexp_path* does not exist. The function raises
            this before it sends an RPC.
        ValueError: The output folder is the default folder of ZEN. The
            function runs nothing.
    """
    czexp_path = Path(czexp_path)
    if not czexp_path.exists():
        raise FileNotFoundError(f"Experiment file not found: {czexp_path}")
    logger = set_logging()
    async with open_zen_channel(config_path) as (channel, metadata):
        exp_service = ExperimentServiceStub(channel=channel, metadata=metadata)

        default_image_folder = await _get_default_image_folder(exp_service, logger)
        image_folder = _resolve_image_folder(custom_image_folder, logger)
        _refuse_zen_default_folder(image_folder, default_image_folder)
        czi_name = _make_czi_basename(custom_filename)

        load_arg = str(czexp_path.with_suffix(""))
        logger.info("Loading Experiment from path: " + load_arg)
        loaded = await exp_service.load(ExperimentServiceLoadRequest(experiment_name=load_arg))
        experiment_id = loaded.experiment_id
        logger.info("Experiment reference id: " + experiment_id)

        result_path = await _run_experiment_and_collect(
            exp_service, experiment_id, default_image_folder, image_folder, czi_name, logger,
        )
        return {"exp_result_path": result_path, "experiment_id": experiment_id}


async def get_running_experiment_status() -> Optional[Dict[str, Any]]:
    """Get the status of the active experiment with ``GetStatus`` and no id.

    An experiment is active if it is an experiment, a snap, a live view or a
    continuous acquisition. A manual stage move is not an experiment. ZEN
    reports an idle microscope with FAILED_PRECONDITION.

    Returns:
        A dict with these keys: is_experiment_running, is_acquisition_running,
        images_acquired_index, images_count, scenes_index and scenes_count.
        The function returns ``None`` if the microscope is idle.

    Raises:
        GRPCError: Each gRPC error that is not FAILED_PRECONDITION.
        Exception: A connection error from ``initialize_zenapi``.
    """
    logger = set_logging()

    async with open_zen_channel(config_path) as (channel, metadata):
        svc = ExperimentServiceStub(channel=channel, metadata=metadata)

        try:
            resp = await svc.get_status(ExperimentServiceGetStatusRequest())
        except GRPCError as e:
            if e.status == Status.FAILED_PRECONDITION:
                logger.info("Experiment status: no experiment running (idle).")
                return None
            raise

        s = resp.status
        logger.info(
            f"Experiment status: experiment_running={s.is_experiment_running} "
            f"acquisition_running={s.is_acquisition_running} "
            f"images={s.images_acquired_index}/{s.images_count}"
        )
        return {
            "is_experiment_running": s.is_experiment_running,
            "is_acquisition_running": s.is_acquisition_running,
            "images_acquired_index": s.images_acquired_index,
            "images_count": s.images_count,
            "scenes_index": s.scenes_index,
            "scenes_count": s.scenes_count,
        }


async def stop_running_experiment() -> bool:
    """Stop the experiment that is active in ZEN, if there is one.

    Use this in a handler for a crash or for Ctrl-C. The function sends
    ``Stop`` with the id of the most recent experiment that this process
    started. If there is no such id, it sends ``Stop`` with no id. We did NOT
    confirm on the microscope that an empty id stops the active experiment.
    The function does not move the stage, Z or the objective.

    Returns:
        ``True`` if the function sent a stop. ``False`` if the microscope is
        idle and the function sent nothing.

    Raises:
        GRPCError: ZEN refused the status request or the stop request.
    """
    logger = set_logging()
    if await get_running_experiment_status() is None:
        logger.info("Stop experiment: no experiment is running.")
        return False

    experiment_id = _last_started_experiment_id or ""
    async with open_zen_channel(config_path) as (channel, metadata):
        svc = ExperimentServiceStub(channel=channel, metadata=metadata)
        logger.warning(f"Stop experiment: sending Stop "
                       f"(experiment id: {experiment_id or 'none - the active one'}).")
        await svc.stop(ExperimentServiceStopRequest(experiment_id=experiment_id))
    logger.warning("Stop experiment: ZEN accepted the stop request.")
    return True
