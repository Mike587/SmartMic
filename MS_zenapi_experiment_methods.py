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
# Wraps the ZEN gRPC ExperimentService (acquisition.v1beta): run a
# .czexp by path, collect the result image, query / stop the running
# experiment.
#################################################################

"""
Experiment acquisition helpers for ZEN blue / ZEN core via gRPC.

This module wraps the ZEN gRPC ``ExperimentService`` (acquisition.v1beta).

Public functions
----------------
run_experiment_from_path      -- Load a ``.czexp`` file by path and run it
get_running_experiment_status -- Status of the active experiment, or None (idle)
stop_running_experiment       -- Stop the active experiment (for a crash / Ctrl-C handler)

Image-output strategy
---------------------
ZEN always saves acquired images to its own default output folder.  After each
acquisition this module moves the file to the caller-supplied *custom_image_folder*
so that downstream code can find images in a predictable location.  Any
leftover ``.czi`` files in the default folder are cleaned up at the end
(DEV_NOTES "The cleanup deletes everything in ZEN's default folder").
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

from MS_run_log import DEFAULT_EXPERIMENT_OUTPUT_FOLDER  # single source for the default output folder
from MS_zenapi_helpers import open_zen_channel, set_logging
from zeiss_paths import CONFIG_PATH as config_path  # config.ini at the repo root

# Auto-generated gRPC stubs for the experiment service.
from zen_api.acquisition.v1beta import (
    ExperimentServiceGetImageOutputPathRequest,
    ExperimentServiceGetStatusRequest,
    ExperimentServiceLoadRequest,
    ExperimentServiceRunExperimentRequest,
    ExperimentServiceStopRequest,
    ExperimentServiceStub,
)

class ExperimentResult(TypedDict):
    """Result of ``run_experiment_from_path``."""
    exp_result_path: Path          # the acquired .czi, in its final folder
    experiment_id: str


# Id of the experiment the latest run of this process started (or None).
# stop_running_experiment uses it: a handler for a crash or Ctrl-C does not
# have the id.
_last_started_experiment_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Shared acquisition helpers
# ---------------------------------------------------------------------------

async def _get_default_image_folder(exp_service: ExperimentServiceStub,
                                    logger: logging.Logger) -> Path:
    """Query ZEN's read-only default image output folder.

    NOTE: this path (e.g. ``F:\\UserData\\mike\\temp``) is **owned by ZEN**, not
    by this repo.  It is whatever is configured as the default image-save
    location inside the running ZEN application, fetched live here.  ZEN always
    writes acquired CZIs here first; the caller then moves each result into the
    custom output folder (see ``_move_result_and_cleanup``).  The ZEN-API exposes
    only ``GetImageOutputPath`` (no setter), so this folder CANNOT be changed from
    code — change it in ZEN's options if needed.  Keeping it on the same drive as
    the custom output folder makes the move a fast same-volume rename.
    """
    save_path = await exp_service.get_image_output_path(
        ExperimentServiceGetImageOutputPathRequest()
    )
    default_image_folder = Path(save_path.image_output_path)
    logger.info("Default Saving Location for CZI Images:" + str(default_image_folder))
    return default_image_folder


def _resolve_image_folder(custom_image_folder: Union[str, Path, None],
                          logger: logging.Logger) -> Path:
    """Resolve and create the target output folder (custom, or the default)."""
    if custom_image_folder is not None:
        image_folder = Path(custom_image_folder)
    else:
        image_folder = DEFAULT_EXPERIMENT_OUTPUT_FOLDER
    image_folder.mkdir(parents=True, exist_ok=True)
    logger.info("Custom Saving Location for CZI Images:" + str(image_folder))
    return image_folder


def _same_folder(a: Path, b: Path) -> bool:
    """True if two paths name the same folder (case and form ignored on Windows)."""
    try:
        return os.path.samefile(a, b)
    except OSError:      # one of them does not exist
        return (os.path.normcase(os.path.abspath(a))
                == os.path.normcase(os.path.abspath(b)))


def _refuse_zen_default_folder(image_folder: Path, default_image_folder: Path) -> None:
    """Raise ValueError if the output folder is ZEN's default image folder.

    After each acquisition the cleanup deletes every other ``.czi`` in ZEN's
    default folder (DEV_NOTES "The cleanup deletes everything in ZEN's default
    folder"). With the results in that same folder, each acquisition would
    delete the results of all earlier ones.
    """
    if _same_folder(image_folder, default_image_folder):
        raise ValueError(
            f"The output folder {image_folder} is ZEN's default image folder. The "
            f"cleanup after each acquisition deletes all other .czi files there, "
            f"so earlier results of this run would be lost. Give another folder "
            f"(custom_image_folder / custom_folder)."
        )


def _make_czi_basename(custom_filename: Optional[str]) -> str:
    """Output base name (without .czi): custom_filename, or a UUID-based name."""
    if custom_filename is not None:
        return custom_filename[:-4] if custom_filename.endswith(".czi") else custom_filename
    return f"zenapi_{str(uuid.uuid4())[:8]}"


def _unique_czi_name(image_folder: Path, czi_name: str, logger: logging.Logger) -> str:
    """Append a running counter so an existing result file is never overwritten."""
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
    """Move the acquired .czi to image_folder and clear leftover .czi files.

    Returns the final Path of the moved (or already in-place) result file.
    """
    exp_default_path = default_image_folder / (exp_result.output_name + ".czi")
    exp_custom_path = image_folder / (exp_result.output_name + ".czi")
    # Resolve before comparing so an equal custom/default folder expressed in a
    # different string form is still detected (and we never rename a file onto
    # itself).
    if exp_default_path.resolve() != exp_custom_path.resolve() and exp_default_path.exists():
        # shutil.move (not Path.rename): the custom folder may be on a different
        # drive than ZEN's default output folder, and os.rename cannot move across
        # drives on Windows (WinError 17). Same-drive moves behave like rename.
        shutil.move(str(exp_default_path), str(exp_custom_path))
        logger.info(f"Moved experiment result from {exp_default_path} to {exp_custom_path}")
        result_path = exp_custom_path
    else:
        result_path = exp_default_path
    logger.info("Final Experiment Location: " + str(result_path))

    # Clean up any leftover .czi files in the ZEN default temp folder.  NEVER
    # delete the result itself: when custom_image_folder IS the default folder
    # the result stays here, so a blanket glob+unlink would destroy the
    # acquisition we just made.
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
    """Run the experiment by id, log status, then move + clean up the result.

    Ensures a non-colliding output name, runs the experiment, logs its status,
    and relocates the result.  Returns the final result Path.
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
# Public API
# ---------------------------------------------------------------------------

async def run_experiment_from_path(
    czexp_path: Union[str, Path],
    custom_image_folder: Union[str, Path, None] = None,
    custom_filename: Optional[str] = None,
) -> ExperimentResult:
    """Load a ``.czexp`` file by path, run it, and move the result out of ZEN's folder.

    ``ExperimentService.Load`` takes a full path WITHOUT the ``.czexp`` suffix.
    Never Import + run instead: an imported tile/region experiment is not
    runnable (ZEN throws NullReferenceException at run).

    Args:
        czexp_path: The ``.czexp`` file.
        custom_image_folder: Folder the ``.czi`` is moved to; None →
            ``DEFAULT_EXPERIMENT_OUTPUT_FOLDER``. ZEN's own default folder is
            refused (``_refuse_zen_default_folder``).
        custom_filename: Output base name; None → ``zenapi_<8 hex>``. An
            existing file gets a counter.

    Raises:
        FileNotFoundError: *czexp_path* does not exist (before any RPC).
        ValueError: The output folder is ZEN's default folder (nothing is run).
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
    """Query the status of the currently active experiment, if any.

    Calls ``ExperimentService.GetStatus`` with no experiment_id.  Per the ZEN
    API contract this returns the status of one active experiment, or raises
    ``FAILED_PRECONDITION`` ("No experiment is running") when nothing is active.

    "Active" covers acquisitions started through ZEN / the API — standard
    experiments, snap, live and continuous.  It does NOT report unrelated
    activity such as a manual stage move outside an experiment.

    Returns:
        A dict of status fields if an experiment is active::

            {
                "is_experiment_running":  bool,
                "is_acquisition_running": bool,
                "images_acquired_index":  int,
                "images_count":           int,
                "scenes_index":           int,
                "scenes_count":           int,
            }

        or ``None`` if the microscope is idle (no experiment running).

    Raises:
        GRPCError:  for any gRPC error other than FAILED_PRECONDITION.
        Exception:  connection errors raised by ``initialize_zenapi``.
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

    For the handler of a crash or of Ctrl-C: the pipeline stops, but ZEN
    continues an acquisition that is already started. Sends
    ``ExperimentService.Stop`` with the id of the experiment the latest
    ``run_experiment_from_path`` call of this process started. If this process started none, the
    request has no id (ZEN's ``GetStatus`` reads the active experiment that
    way; that ``Stop`` does the same is NOT verified on the scope).

    Stop ends the acquisition. It does not move the stage, Z or the objective.

    Returns:
        True if an experiment was active and the stop request was sent, False
        if the microscope was idle (nothing is sent then).

    Raises:
        GRPCError: ZEN refused the status query or the stop request.
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
