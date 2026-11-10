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
# File        : MS_zenapi_experiment_smoke.py
# Modified by : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#
# Smoke test of the ZEN gRPC ExperimentService (acquisition.v1beta):
# one call that exercises every experiment method against a live gateway.
#################################################################

"""
Smoke test of the ZEN ``ExperimentService`` — NOT an acquisition path.

:func:`check_experiment_api` exercises *every* experiment method in one call
(list, load, clone, save, export, import, delete, optional snap / live /
continuous, run).  Use it to validate the API against a live gateway, e.g.
after a ZEN update.  Do not call it for every image: it runs a full
serialization round-trip and leaves an imported experiment loaded in ZEN.

The acquisition functions a pipeline uses are in
``MS_zenapi_experiment_methods`` (``run_experiment_by_name`` / ``_from_path`` /
``_from_xml``).  Split out of that module on 2026-11-10.

It moves hardware: the experiment is run, and with ``do_snap_and_live`` a snap,
live and continuous acquisition are started.
"""

import asyncio
import os
import shutil
from pathlib import Path
from typing import Dict, Union

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
from MS_zenapi_helpers import open_zen_channel, set_logging
from MS_zenapi_experiment_methods import (
    _get_default_image_folder,
    _make_czi_basename,
    _resolve_image_folder,
    _run_experiment_and_collect,
)
from zeiss_paths import CONFIG_PATH as config_path  # config.ini at the repo root

from zen_api.acquisition.v1beta import (
    ExperimentServiceCloneRequest,
    ExperimentServiceDeleteRequest,
    ExperimentServiceExportRequest,
    ExperimentServiceGetAvailableExperimentsRequest,
    ExperimentServiceImportRequest,
    ExperimentServiceLoadRequest,
    ExperimentServiceRunSnapRequest,
    ExperimentServiceSaveRequest,
    ExperimentServiceStartContinuousRequest,
    ExperimentServiceStartLiveRequest,
    ExperimentServiceStopRequest,
    ExperimentServiceStub,
)

# Folder where ZEN stores the .czexp files of the logged-in ZEN user. Used only
# to remove a stale test clone before the save, and to verify its deletion.
# Machine- and user-specific: override with SMARTMIC_ZEN_EXPERIMENT_FOLDER.
ZEN_EXPERIMENT_FOLDER = Path(os.environ.get(
    "SMARTMIC_ZEN_EXPERIMENT_FOLDER",
    r"C:\ProgramData\Carl Zeiss\ZEN\Users\mike\Documents\Experiment Setups",
))

# Name for the temporary cloned experiment (created, then deleted again).
CLONED_EXPERIMENT_NAME = "deleteme_0011"

# Seconds to wait after stopping Continuous before the next call.
# TODO: remove once the ZEN gRPC server handles this internally.
STOP_CONTINUOUS_WAIT_S = 3


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
        configfile: Path to the ZEN API ``config.ini`` file. None → the one at
            the repo root.
        custom_image_folder: Directory where acquired images are placed after
            ZEN saves them to its default output folder. None →
            ``MS_run_log.DEFAULT_EXPERIMENT_OUTPUT_FOLDER``.
        custom_filename: Base filename (without ``.czi``) for the snap and the
            experiment output.  None → a generated name.
        do_snap_and_live: When ``True``, also performs a snap, live, and
            continuous acquisition before running the full experiment.

    Returns:
        Dictionary with keys:

        * ``"snap_path"``       -- Path to the snap image, or ``None`` if
          *do_snap_and_live* was ``False``.
        * ``"exp_result_path"`` -- Path to the experiment result ``.czi`` file.
        * ``"experiment_id"``   -- reference id of the loaded experiment.

    Raises:
        Exception: Re-raises any exception from ``exp_service.load`` after
            logging the list of available experiments to aid debugging.
    """
    results = {}
    logger = set_logging()
    clone_file = ZEN_EXPERIMENT_FOLDER / (CLONED_EXPERIMENT_NAME + ".czexp")

    async with open_zen_channel(configfile or config_path) as (channel, metadata):
        logger.info("Create gRPC Channel and ExperimentService ...")
        exp_service = ExperimentServiceStub(channel=channel, metadata=metadata)

        default_image_folder = await _get_default_image_folder(exp_service, logger)
        image_folder = _resolve_image_folder(custom_image_folder, logger)
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

        logger.info("Loading Experiment ...")
        try:
            my_exp = await exp_service.load(
                ExperimentServiceLoadRequest(experiment_name=experiment_name)
            )
            logger.info(
                "ExperimentName:" + experiment_name + " Reference Id: " + my_exp.experiment_id
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
        if clone_file.exists():
            clone_file.unlink()
            logger.info("Overwrite experiment:" + clone_file.name)

        logger.info("Saving Experiment ...")
        await exp_service.save(
            ExperimentServiceSaveRequest(
                experiment_id=my_exp_cloned.experiment_id,
                experiment_name=CLONED_EXPERIMENT_NAME,
            )
        )

        logger.info("Exporting Experiment as XML String ...")
        exp_xml = await exp_service.export(
            ExperimentServiceExportRequest(experiment_id=my_exp_cloned.experiment_id)
        )
        # A short excerpt, so the XML structure is visible in the run log.
        logger.info(exp_xml.xml[:300])

        logger.info("Importing Experiment from XML String ...")
        imported_exp = await exp_service.import_(ExperimentServiceImportRequest(exp_xml.xml))
        logger.info("Reference Id (imported): " + imported_exp.experiment_id)

        logger.info("Delete cloned Experiment ...")
        await exp_service.delete(
            ExperimentServiceDeleteRequest(experiment_name=CLONED_EXPERIMENT_NAME))

        if not clone_file.exists():
            logger.info("Deleted experiment:" + clone_file.name)

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

            # Move the snap to the custom folder if the paths differ.
            snap_default_path = default_image_folder / (snap.output_name + ".czi")
            snap_custom_path = image_folder / (snap.output_name + ".czi")
            if snap_default_path.resolve() != snap_custom_path.resolve() and snap_default_path.exists():
                # shutil.move (not Path.rename): the custom folder may be on a
                # different drive than ZEN's default folder.
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
            # No wait here, unlike after the stop of Continuous below. Whether
            # the same stop latency applies to this stop → start_continuous
            # transition is not tested on hardware. If start_continuous fails
            # here, try the same asyncio.sleep(STOP_CONTINUOUS_WAIT_S) first.

            logger.info("Starting Continuous ...")
            await exp_service.start_continuous(
                ExperimentServiceStartContinuousRequest(experiment_id=my_exp.experiment_id)
            )

            logger.info("Stopping Continuous ...")
            await exp_service.stop(ExperimentServiceStopRequest(experiment_id=my_exp.experiment_id))
            await asyncio.sleep(STOP_CONTINUOUS_WAIT_S)

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
