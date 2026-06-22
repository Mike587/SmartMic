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
# Wraps the ZEN gRPC ExperimentService (acquisition.v1beta) to provide
# a single high-level function that exercises the full experiment
# lifecycle: list, load, clone, save, export, import, delete, snap,
# live, continuous, and run-experiment.
#################################################################

"""
Experiment lifecycle helpers for ZEN blue / ZEN core via gRPC.

This module wraps the ZEN gRPC ``ExperimentService`` (acquisition.v1beta).

Two kinds of entry point:

* **Acquisition** (production) — :func:`run_experiment_by_name`,
  :func:`run_experiment_from_path`, :func:`run_experiment_from_xml`.  Each loads
  (or imports) a runnable experiment, runs it, and collects the result.  These
  are the lean paths the SmartMic pipeline uses for every image.
* **Smoke test** — :func:`check_experiment_api`, which exercises *every*
  available experiment method in one call (load, clone, save, export, import,
  delete, optional snap/live/continuous, run).  Use it to validate the API
  against a live gateway; it is NOT the per-image acquisition path (it runs a
  full serialization round-trip and leaves an imported experiment loaded in
  ZEN, which would accumulate if called for every image).

Image-output strategy
---------------------
ZEN always saves acquired images to its own default output folder.  After each
acquisition this module moves the file to the caller-supplied *custom_image_folder*
so that downstream code can find images in a predictable location.  Any
leftover ``.czi`` files in the default folder are cleaned up at the end.
"""

import asyncio
import shutil
from typing import Dict, Union, Optional
from pathlib import Path
import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
from MS_zenapi_helpers import set_logging, open_zen_channel
from MS_Helper_function import DEFAULT_EXPERIMENT_OUTPUT_FOLDER  # single source for the default output folder
from grpclib import GRPCError
from grpclib.const import Status
import uuid

# Module-level constants used when the script is run directly (not via API).
# exp_folder = Path(r"f:\Documents\Carl Zeiss\ZENCore\Documents\Experiment Setups")
exp_folder = Path(r"C:\ProgramData\Carl Zeiss\ZEN\Users\mike\Documents\Experiment Setups")

# Name for the temporary cloned experiment created during testing.
exp_cloned_name = "deleteme_0011"

# Fallback output folder used when the script is executed directly.
# When called as a library function, pass custom_image_folder instead.
image_folder = DEFAULT_EXPERIMENT_OUTPUT_FOLDER
image_folder.mkdir(parents=True, exist_ok=True)

# Path-existence debug snippet (disabled; kept as a useful reference).
'''
test = Path(image_folder)
print(test.exists())
test = Path(exp_folder)
print(test.exists())
'''

# Auto-generated gRPC stubs for the experiment service.
from zen_api.acquisition.v1beta import (
    ExperimentServiceStub,
    ExperimentServiceLoadRequest,
    ExperimentServiceGetImageOutputPathRequest,
    ExperimentServiceGetAvailableExperimentsRequest,
    ExperimentServiceCloneRequest,
    ExperimentServiceSaveRequest,
    ExperimentServiceExportRequest,
    ExperimentServiceImportRequest,
    ExperimentServiceDeleteRequest,
    ExperimentServiceRunSnapRequest,
    ExperimentServiceStartLiveRequest,
    ExperimentServiceStopRequest,
    ExperimentServiceStartContinuousRequest,
    ExperimentServiceRunExperimentRequest,
    ExperimentServiceGetStatusRequest,
)

# How many seconds to wait after stopping Continuous before the next call.
# TODO: remove once the ZEN gRPC server handles this internally.
waittime = 3

# Set to True in __main__ to open and display the acquired CZI image.
open_czi = False

# config.ini path — single-sourced from zeiss_paths (repo root), not recomputed.
from zeiss_paths import CONFIG_PATH as config_path


# ---------------------------------------------------------------------------
# Shared acquisition helpers
# ---------------------------------------------------------------------------
# The three public run_* functions below differ only in HOW they obtain a
# runnable experiment_id (load by name / import XML / load by path).  Everything
# around the actual run — resolving the output folder, naming the file, running
# it, then moving the result out of ZEN's default folder and cleaning up — is
# identical and lives in these helpers.

async def _get_default_image_folder(exp_service, logger) -> Path:
    """Query ZEN's read-only default image output folder."""
    save_path = await exp_service.get_image_output_path(
        ExperimentServiceGetImageOutputPathRequest()
    )
    default_image_folder = Path(save_path.image_output_path)
    logger.info("Default Saving Location for CZI Images:" + str(default_image_folder))
    return default_image_folder


def _resolve_image_folder(custom_image_folder, logger) -> Path:
    """Resolve and create the target output folder (custom, or the default)."""
    if custom_image_folder is not None:
        image_folder = Path(custom_image_folder)
    else:
        image_folder = DEFAULT_EXPERIMENT_OUTPUT_FOLDER
    image_folder.mkdir(parents=True, exist_ok=True)
    logger.info("Custom Saving Location for CZI Images:" + str(image_folder))
    return image_folder


def _make_czi_basename(custom_filename) -> str:
    """Output base name (without .czi): custom_filename, or a UUID-based name."""
    if custom_filename is not None:
        return custom_filename[:-4] if custom_filename.endswith(".czi") else custom_filename
    return f"zenapi_{str(uuid.uuid4())[:8]}"


def _unique_czi_name(image_folder, czi_name, logger) -> str:
    """Append a running counter so an existing result file is never overwritten."""
    base_czi_name = czi_name
    counter = 1
    while (image_folder / (czi_name + ".czi")).exists():
        czi_name = f"{base_czi_name}_{counter:06d}"
        counter += 1
    if czi_name != base_czi_name:
        logger.info(f"File {base_czi_name}.czi already exists. Using {czi_name}.czi instead.")
    return czi_name


def _move_result_and_cleanup(exp_result, default_image_folder, image_folder, logger) -> Path:
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
    exp_service, experiment_id, default_image_folder, image_folder, czi_name, logger
) -> Path:
    """Run the experiment by id, log status, then move + clean up the result.

    Shared tail of all three run_* functions: ensures a non-colliding output
    name, runs the experiment, logs its status, and relocates the result.
    Returns the final result Path.
    """
    czi_name = _unique_czi_name(image_folder, czi_name, logger)
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


async def get_running_experiment_status() -> Optional[Dict]:
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


async def check_experiment_api(
    experiment_name: str,
    configfile: Union[str, Path, None] = None,
    custom_image_folder: Union[str, Path, None] = None,
    custom_filename: Union[str, None] = None,
    do_snap_and_live: bool = False,
) -> Dict[str, Union[str, Path, None]]:
    """Exercise the full ZEN ExperimentService API and run a single acquisition.

    Performs the following steps in order:

    1. Query available experiments and their default output path.
    2. Load the requested experiment by name.
    3. Clone it, save the clone under a fixed test name, export to XML,
       re-import from XML, then delete the clone — verifying round-trip
       serialisation.
    4. Optionally run a snap, start/stop live, and start/stop continuous
       (controlled by *do_snap_and_live*).
    5. Run the full experiment and move the resulting ``.czi`` to
       *custom_image_folder*.
    6. Clean up any stray ``.czi`` files left in the ZEN default output folder.

    Args:
        experiment_name: Name of the experiment to load (without the
            ``.czexp`` extension).
        configfile: Path to the ZEN API ``config.ini`` file. When ``None``
            (the default) it resolves to ``config.ini`` next to this script,
            independent of the current working directory.
        custom_image_folder: Directory where acquired images should be
            placed after ZEN saves them to its default output folder.
            Defaults to ``F:/UserData/mike/api`` when ``None``.
        custom_filename: Base filename (without ``.czi``) to use for both
            the snap and experiment output.  When ``None`` a UUID-based name
            is generated to avoid collisions.
        do_snap_and_live: When ``True``, also performs a snap, live, and
            continuous acquisition before running the full experiment.
            Defaults to ``False`` to keep the call fast in production.

    Returns:
        Dictionary with keys:

        * ``"snap_path"``       -- :class:`~pathlib.Path` to the snap image,
          or ``None`` if *do_snap_and_live* was ``False``.
        * ``"exp_result_path"`` -- :class:`~pathlib.Path` to the experiment
          result ``.czi`` file.
        * ``"experiment_id"``   -- reference id of the loaded experiment
          (matches ``run_experiment_from_path`` / ``run_experiment_from_xml``).

    Raises:
        Exception: Re-raises any exception from ``exp_service.load`` after
            logging the list of available experiments to aid debugging.
    """
    my_experiment = experiment_name
    results = {}

    logger = set_logging()

    # Resolve config.ini relative to this script (not the CWD) when not given.
    if configfile is None:
        configfile = config_path
    async with open_zen_channel(configfile) as (channel, metadata):
        logger.info("Create gRPC Channel and ExperimentService ...")
        exp_service = ExperimentServiceStub(channel=channel, metadata=metadata)

        # Query where ZEN saves images by default, and resolve the target folder.
        default_image_folder = await _get_default_image_folder(exp_service, logger)
        image_folder = _resolve_image_folder(custom_image_folder, logger)

        # Build output file base names.  Use the shared _make_czi_basename helper
        # so the default scheme matches run_experiment_from_xml /
        # run_experiment_from_path (zenapi_<uuid>); the snap reuses that base.
        czi_name = _make_czi_basename(custom_filename)
        snap_output_name = f"{czi_name}_snap"

        # List available experiments for reference and error reporting.
        available_experiments = await exp_service.get_available_experiments(
            ExperimentServiceGetAvailableExperimentsRequest()
        )
        logger.info(
            f"Number of available Experiment File(s) inside ZEN folder: "
            f"{len(available_experiments.experiments)}"
        )

        # for exp in available_experiments.experiments:
        #    logger.info(exp.name + ".czexp")

        logger.info("Loading Experiment ...")
        try:
            my_exp = await exp_service.load(
                ExperimentServiceLoadRequest(experiment_name=experiment_name)
            )
            logger.info(
                "ExperimentName:" + my_experiment + " Reference Id: " + my_exp.experiment_id
            )
        except Exception as e:
            logger.error(f"Failed to load experiment '{experiment_name}': {e}")
            logger.info("Available experiments:")
            for exp in available_experiments.experiments:
                logger.info(f"  - {exp.name}")
            raise

        # --- Clone / save / export / import / delete round-trip ---

        logger.info("Cloning Experiment ...")
        my_exp_cloned = await exp_service.clone(
            ExperimentServiceCloneRequest(experiment_id=my_exp.experiment_id)
        )

        # Remove any stale version of the test clone before saving.
        if Path(exp_folder / (exp_cloned_name + ".czexp")).exists():
            Path(exp_folder / (exp_cloned_name + ".czexp")).unlink()
            logger.info("Overwrite experiment:" + exp_cloned_name + ".czexp")

        logger.info("Saving Experiment ...")
        await exp_service.save(
            ExperimentServiceSaveRequest(
                experiment_id=my_exp_cloned.experiment_id,
                experiment_name=exp_cloned_name,
            )
        )

        logger.info("Exporting Experiment as XML String ...")
        exp_xml = await exp_service.export(
            ExperimentServiceExportRequest(experiment_id=my_exp_cloned.experiment_id)
        )
        # Log a short excerpt so the XML structure is visible in the run log.
        print(exp_xml.xml[:300])

        logger.info("Importing Experiment from XML String ...")
        imported_exp = await exp_service.import_(ExperimentServiceImportRequest(exp_xml.xml))
        logger.info("Reference Id (imported): " + imported_exp.experiment_id)

        logger.info("Delete cloned Experiment ...")
        await exp_service.delete(ExperimentServiceDeleteRequest(experiment_name=exp_cloned_name))

        if not Path(exp_folder / (exp_cloned_name + ".czexp")).exists():
            logger.info("Deleted experiment:" + exp_cloned_name + ".czexp")

        # --- Optional snap / live / continuous ---

        if do_snap_and_live:
            logger.info("Start SNAP Experiment ...")

            temp_files_before = list(default_image_folder.glob("*.czi"))
            if temp_files_before:
                logger.warning(
                    f"Default folder {default_image_folder} contains "
                    f"{len(temp_files_before)} CZI files before snap. "
                    "They will be cleaned up later."
                )

            # output_name must be a plain filename without extension.  ZEN writes
            # the file to default_image_folder; we move it afterwards.
            snap = await exp_service.run_snap(
                ExperimentServiceRunSnapRequest(
                    experiment_id=my_exp.experiment_id,
                    output_name=snap_output_name,
                )
            )

            snap_default_path = default_image_folder / (snap.output_name + ".czi")
            print("snap default path:")
            print(snap_default_path)

            # Move the snap to the custom folder if the paths differ.
            snap_custom_path = image_folder / (snap.output_name + ".czi")
            if snap_default_path != snap_custom_path and snap_default_path.exists():
                # shutil.move (not Path.rename) — see _move_result_and_cleanup:
                # the custom folder may be on a different drive than ZEN's default.
                shutil.move(str(snap_default_path), str(snap_custom_path))
                logger.info(f"Moved snap from {snap_default_path} to {snap_custom_path}")
                results["snap_path"] = snap_custom_path
            else:
                results["snap_path"] = snap_default_path

            logger.info("Final Snap Location: " + str(results["snap_path"]))

            logger.info("Starting Live ...")
            await exp_service.start_live(
                ExperimentServiceStartLiveRequest(
                    experiment_id=my_exp.experiment_id, track_index=0
                )
            )

            logger.info("Stopping Live ...")
            await exp_service.stop(ExperimentServiceStopRequest(experiment_id=my_exp.experiment_id))
            # await asyncio.sleep(waittime)  # may be needed once ZEN gRPC handles stop latency

            logger.info("Starting Continuous ...")
            await exp_service.start_continuous(
                ExperimentServiceStartContinuousRequest(experiment_id=my_exp.experiment_id)
            )

            logger.info("Stopping Continuous ...")
            await exp_service.stop(ExperimentServiceStopRequest(experiment_id=my_exp.experiment_id))
            # TODO: remove this sleep once ZEN gRPC handles stop latency internally.
            await asyncio.sleep(waittime)

        else:
            logger.info("Skipping snap, live, and continuous acquisition steps.")
            results["snap_path"] = None

        # --- Full experiment run ---

        temp_files_before_exp = list(default_image_folder.glob("*.czi"))
        if temp_files_before_exp:
            logger.warning(
                f"Default folder {default_image_folder} contains "
                f"{len(temp_files_before_exp)} CZI files before experiment. "
                "They will be cleaned up after moving."
            )

        results["exp_result_path"] = await _run_experiment_and_collect(
            exp_service, my_exp.experiment_id, default_image_folder, image_folder,
            czi_name, logger,
        )

        results["experiment_id"] = my_exp.experiment_id
        return results


def _normalize_experiment_xml(xml: str) -> str:
    """Strip a UTF-8 BOM and the ``<?xml ...?>`` declaration from experiment XML.

    ZEN's ``ExperimentService.Import`` expects the string to start at
    ``<HardwareExperiment>`` (that is how ``Export`` returns it).  On-disk
    ``.czexp`` files prepend a BOM and an ``<?xml ...?>`` prolog, which the
    importer rejects with ``INVALID_ARGUMENT``.  This makes either form work.
    """
    if xml and xml[0] == "﻿":      # strip BOM if present
        xml = xml[1:]
    idx = xml.find("<HardwareExperiment")
    if idx > 0:
        xml = xml[idx:]
    return xml


async def run_experiment_from_xml(
    xml: str,
    configfile: Union[str, Path, None] = None,
    custom_image_folder: Union[str, Path, None] = None,
    custom_filename: Union[str, None] = None,
) -> Dict[str, Union[str, Path, None]]:
    """Import an experiment from an XML string and run it.

    This is the core primitive: it imports *xml* via ``ExperimentService.Import``
    and runs the imported experiment.  The XML may be the raw
    ``<HardwareExperiment>`` string (as returned by ``Export`` or built on the
    fly) or full ``.czexp`` file content — a leading BOM / ``<?xml ...?>``
    prolog is normalized away.

    Args:
        xml: The experiment XML string.
        configfile: ZEN API ``config.ini`` path.  When ``None`` (the default)
            it resolves to ``config.ini`` next to this script, independent of
            the current working directory.
        custom_image_folder: Directory the acquired ``.czi`` is moved to after
            ZEN writes it to its default output folder.  Defaults to
            ``F:/UserData/mike/api`` when ``None``.
        custom_filename: Output base name (without ``.czi``).  A UUID-based
            name is generated when ``None``.

    Returns:
        Dict with ``"exp_result_path"`` (Path to the result ``.czi``),
        ``"snap_path"`` (always ``None`` here), and ``"experiment_id"`` (the
        imported experiment's reference id).
    """
    logger = set_logging()
    results: Dict[str, Union[str, Path, None]] = {}

    xml = _normalize_experiment_xml(xml)

    # Resolve config.ini relative to this script (not the CWD) when not given.
    if configfile is None:
        configfile = config_path
    async with open_zen_channel(configfile) as (channel, metadata):
        logger.info("Create gRPC Channel and ExperimentService ...")
        exp_service = ExperimentServiceStub(channel=channel, metadata=metadata)

        default_image_folder = await _get_default_image_folder(exp_service, logger)
        image_folder = _resolve_image_folder(custom_image_folder, logger)
        czi_name = _make_czi_basename(custom_filename)

        # Import the experiment from the XML string.
        logger.info("Importing Experiment from XML string ...")
        imported_exp = await exp_service.import_(ExperimentServiceImportRequest(xml))
        logger.info("Reference Id (imported): " + imported_exp.experiment_id)

        results["exp_result_path"] = await _run_experiment_and_collect(
            exp_service, imported_exp.experiment_id, default_image_folder, image_folder,
            czi_name, logger,
        )

        results["snap_path"] = None
        results["experiment_id"] = imported_exp.experiment_id
        return results


async def run_experiment_from_path(
    czexp_path: Union[str, Path],
    configfile: Union[str, Path, None] = None,
    custom_image_folder: Union[str, Path, None] = None,
    custom_filename: Union[str, None] = None,
) -> Dict[str, Union[str, Path, None]]:
    """Load an experiment from a ``.czexp`` file path and run it.

    ZEN's ``ExperimentService.Load`` accepts a FULL PATH (without the ``.czexp``
    extension), not just a name inside ZEN's experiment folder — so a per-run
    modified experiment at any path can be loaded and run directly.

    IMPORTANT: this LOADS the experiment (like :func:`check_experiment_api`) and
    runs the loaded id.  It does NOT import the XML and run the imported id —
    an imported experiment is not in a runnable state and ZEN throws a
    NullReferenceException ("Object reference not set to an instance of an
    object") at run for tile/regions experiments.

    Args:
        czexp_path: Path to the ``.czexp`` file to load and run.
        configfile: ZEN API ``config.ini`` path.  When ``None`` (the default)
            it resolves to ``config.ini`` next to this script, independent of
            the current working directory.
        custom_image_folder: Directory the acquired ``.czi`` is moved to.
            Defaults to ``F:/UserData/mike/api`` when ``None``.
        custom_filename: Output base name (without ``.czi``).

    Returns:
        Dict with ``"exp_result_path"``, ``"snap_path"`` (None) and
        ``"experiment_id"`` (the loaded experiment's reference id).

    Raises:
        FileNotFoundError: if *czexp_path* does not exist.
    """
    logger = set_logging()
    results: Dict[str, Union[str, Path, None]] = {}

    czexp_path = Path(czexp_path)
    if not czexp_path.exists():
        raise FileNotFoundError(f"Experiment file not found: {czexp_path}")

    # Resolve config.ini relative to this script (not the CWD) when not given.
    if configfile is None:
        configfile = config_path
    async with open_zen_channel(configfile) as (channel, metadata):
        logger.info("Create gRPC Channel and ExperimentService ...")
        exp_service = ExperimentServiceStub(channel=channel, metadata=metadata)

        default_image_folder = await _get_default_image_folder(exp_service, logger)
        image_folder = _resolve_image_folder(custom_image_folder, logger)
        czi_name = _make_czi_basename(custom_filename)

        # Load BY PATH (without the .czexp extension) → runnable experiment.
        load_arg = str(czexp_path.with_suffix(""))
        logger.info("Loading Experiment from path: " + load_arg)
        loaded_exp = await exp_service.load(
            ExperimentServiceLoadRequest(experiment_name=load_arg)
        )
        logger.info("Reference Id (loaded): " + loaded_exp.experiment_id)

        results["exp_result_path"] = await _run_experiment_and_collect(
            exp_service, loaded_exp.experiment_id, default_image_folder, image_folder,
            czi_name, logger,
        )

        results["snap_path"] = None
        results["experiment_id"] = loaded_exp.experiment_id
        return results


async def run_experiment_by_name(
    experiment_name: str,
    configfile: Union[str, Path, None] = None,
    custom_image_folder: Union[str, Path, None] = None,
    custom_filename: Union[str, None] = None,
) -> Dict[str, Union[str, Path, None]]:
    """Load an experiment by name and run it — the lean production acquisition path.

    This is what the SmartMic pipeline calls for every image: load the named
    experiment, run it, move the result to *custom_image_folder*, and clean up.
    It deliberately does NOT perform the clone / save / export / import / delete
    round-trip or the snap / live / continuous steps that
    :func:`check_experiment_api` does — those belong to the API smoke test, not
    to a per-image acquisition.  (The round-trip also leaves an imported
    experiment loaded in ZEN on every call, which would accumulate if it ran for
    every image.)

    Args:
        experiment_name: Name of the experiment to load (without the ``.czexp``
            extension).
        configfile: ZEN API ``config.ini`` path.  When ``None`` (the default) it
            resolves to ``config.ini`` next to this script, independent of the
            current working directory.
        custom_image_folder: Directory the acquired ``.czi`` is moved to after
            ZEN writes it to its default output folder.  Defaults to
            ``F:/UserData/mike/api`` when ``None``.
        custom_filename: Output base name (without ``.czi``).  A UUID-based name
            is generated when ``None``.

    Returns:
        Dict with ``"exp_result_path"`` (Path to the result ``.czi``),
        ``"snap_path"`` (always ``None`` here), and ``"experiment_id"`` (the
        loaded experiment's reference id).  Same shape as
        :func:`check_experiment_api` and the other ``run_experiment_*`` helpers.
    """
    logger = set_logging()
    results: Dict[str, Union[str, Path, None]] = {}

    # Resolve config.ini relative to this script (not the CWD) when not given.
    if configfile is None:
        configfile = config_path
    async with open_zen_channel(configfile) as (channel, metadata):
        logger.info("Create gRPC Channel and ExperimentService ...")
        exp_service = ExperimentServiceStub(channel=channel, metadata=metadata)

        default_image_folder = await _get_default_image_folder(exp_service, logger)
        image_folder = _resolve_image_folder(custom_image_folder, logger)
        czi_name = _make_czi_basename(custom_filename)

        # Load BY NAME → runnable experiment (no import round-trip).
        logger.info("Loading Experiment ...")
        loaded_exp = await exp_service.load(
            ExperimentServiceLoadRequest(experiment_name=experiment_name)
        )
        logger.info("Reference Id (loaded): " + loaded_exp.experiment_id)

        results["exp_result_path"] = await _run_experiment_and_collect(
            exp_service, loaded_exp.experiment_id, default_image_folder, image_folder,
            czi_name, logger,
        )

        results["snap_path"] = None
        results["experiment_id"] = loaded_exp.experiment_id
        return results


if __name__ == "__main__":
    logger = set_logging()

    results = asyncio.run(
        check_experiment_api(experiment_name="DAPI_GFP_001", configfile=config_path)
    )
    logger.info(results)

    if open_czi:
        # Imported here (not at module top) since they are only needed for the
        # optional CZI display when running this module directly.
        from pylibCZIrw import czi as pyczi
        from matplotlib import pyplot as plt
        import matplotlib.cm as cm

        with pyczi.open_czi(str(results["exp_result_path"])) as czidoc:
            t = 0
            c = 0
            s = 0
            z = 0

            img2d = czidoc.read(plane={"C": c, "T": t, "Z": z}, scene=s)
            logger.info(f"Shape of 2D plane: {img2d.shape}")

            total_bounding_box = czidoc.total_bounding_box
            logger.info(f"Total BBox: {total_bounding_box}")

        logger.info("Displaying CZI image data ...")
        fig1, ax = plt.subplots(1, 1, figsize=(12, 8))
        ax.imshow(img2d[..., 0], cmap=cm.inferno, vmin=100, vmax=5000)
        ax.set_title(f"{results['exp_result_path']}: S={s} T={t} C={c} Z={z}")
        plt.show()
