# -*- coding: utf-8 -*-

#################################################################
# File        : zenapi_experiment_methods.py
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
The main entry point is :func:`check_experiment_api`, which exercises every
available experiment method in a single call.  It is used both as a smoke-test
and as the acquisition back-end in the SmartMic pipeline.

Image-output strategy
---------------------
ZEN always saves acquired images to its own default output folder.  After each
acquisition this module moves the file to the caller-supplied *custom_image_folder*
so that downstream code can find images in a predictable location.  Any
leftover ``.czi`` files in the default folder are cleaned up at the end.
"""

import asyncio
from pylibCZIrw import czi as pyczi
from matplotlib import pyplot as plt
import matplotlib.cm as cm
from typing import Dict, Union, Optional
from pathlib import Path
import time
import zeiss_paths  # noqa: F401  — extends sys.path so zen_api / zen_api_utils resolve
from zen_api_utils.misc import set_logging, initialize_zenapi
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
global image_folder
image_folder = Path("F:/UserData/mike/api")
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

# Resolve config.ini relative to this script so the module works regardless
# of the current working directory.
script_dir = Path(__file__).parent
config_path = script_dir / "config.ini"


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

    channel, metadata = initialize_zenapi(config_path)
    svc = ExperimentServiceStub(channel=channel, metadata=metadata)

    try:
        resp = await svc.get_status(ExperimentServiceGetStatusRequest())
    except GRPCError as e:
        if e.status == Status.FAILED_PRECONDITION:
            logger.info("Experiment status: no experiment running (idle).")
            return None
        raise
    finally:
        channel.close()

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
    configfile: str = "config.ini",
    custom_image_folder: Union[str, Path, None] = None,
    custom_filename: Union[str, None] = None,
    do_snap_and_live: bool = False,
) -> Dict[str, Union[str, Path]]:
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
        configfile: Path to the ZEN API ``config.ini`` file, or a filename
            relative to the script directory (default ``"config.ini"``).
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

    Raises:
        Exception: Re-raises any exception from ``exp_service.load`` after
            logging the list of available experiments to aid debugging.
    """
    my_experiment = experiment_name
    results = {}

    logger = set_logging()

    channel, metadata = initialize_zenapi(configfile)

    logger.info("Create gRPC Channel and ExperimentService ...")
    exp_service = ExperimentServiceStub(channel=channel, metadata=metadata)

    # Query where ZEN saves images by default (read-only; cannot be changed via API).
    save_path = await exp_service.get_image_output_path(
        ExperimentServiceGetImageOutputPathRequest()
    )
    logger.info("Default Saving Location for CZI Images:" + str(save_path.image_output_path))
    default_image_folder = Path(save_path.image_output_path)

    # Resolve the target output folder for this call.
    if custom_image_folder is not None:
        image_folder = Path(custom_image_folder)
    else:
        image_folder = Path("F:/UserData/mike/api")
    image_folder.mkdir(parents=True, exist_ok=True)
    logger.info("Custom Saving Location for CZI Images:" + str(image_folder))

    # Build output file base names.
    if custom_filename is not None:
        custom_base = custom_filename
        if custom_base.endswith('.czi'):
            custom_base = custom_base[:-4]
        czi_name = custom_base
        snap_output_name = f"{custom_base}_snap"
    else:
        # UUID suffix ensures uniqueness across concurrent pipeline runs.
        unique_id = str(uuid.uuid4())[:8]
        czi_name = f"zenapi_myimage_{unique_id}"  # without .czi extension
        snap_output_name = f"snap_image_{unique_id}"

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
            snap_default_path.rename(snap_custom_path)
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
        # time.sleep(waittime)  # may be needed once ZEN gRPC handles stop latency

        logger.info("Starting Continuous ...")
        await exp_service.start_continuous(
            ExperimentServiceStartContinuousRequest(experiment_id=my_exp.experiment_id)
        )

        logger.info("Stopping Continuous ...")
        await exp_service.stop(ExperimentServiceStopRequest(experiment_id=my_exp.experiment_id))
        # TODO: remove this sleep once ZEN gRPC handles stop latency internally.
        time.sleep(waittime)

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

    # Avoid overwriting an existing file by appending a running counter.
    base_czi_name = czi_name
    counter = 1
    while True:
        full_image_path = Path(image_folder / (czi_name + ".czi"))
        if not full_image_path.exists():
            break
        czi_name = f"{base_czi_name}_{counter:06d}"
        counter += 1

    # Keep snap name in sync if a custom filename was originally given.
    if custom_filename is not None:
        snap_output_name = f"{czi_name}_snap"

    if czi_name != base_czi_name:
        logger.info(
            f"File {base_czi_name}.czi already exists. Using {czi_name}.czi instead."
        )
    else:
        logger.info(str(full_image_path) + " does not exist yet in custom folder")

    logger.info("Starting Experiment Execution ...")
    logger.info(f"Using output name: {czi_name}")

    exp_result = await exp_service.run_experiment(
        ExperimentServiceRunExperimentRequest(
            experiment_id=my_exp.experiment_id, output_name=czi_name
        )
    )

    exp_status = await exp_service.get_status(
        ExperimentServiceGetStatusRequest(experiment_id=my_exp.experiment_id)
    )
    logger.info(exp_status)

    # Move the result from the ZEN default folder to the custom folder.
    exp_default_path = default_image_folder / (exp_result.output_name + ".czi")
    exp_custom_path = image_folder / (exp_result.output_name + ".czi")
    if exp_default_path != exp_custom_path and exp_default_path.exists():
        exp_default_path.rename(exp_custom_path)
        logger.info(f"Moved experiment result from {exp_default_path} to {exp_custom_path}")
        results["exp_result_path"] = exp_custom_path
    else:
        results["exp_result_path"] = exp_default_path

    logger.info("Final Experiment Location: " + str(results["exp_result_path"]))

    # Clean up any leftover .czi files in the ZEN default temp folder.
    temp_files_after = list(default_image_folder.glob("*.czi"))
    for temp_file in temp_files_after:
        try:
            temp_file.unlink()
            logger.info(f"Cleaned up temp file: {temp_file}")
        except Exception as e:
            logger.warning(f"Could not delete temp file {temp_file}: {e}")
    logger.info(f"Cleaned up {len(temp_files_after)} files from default temp folder.")

    channel.close()
    return results


if __name__ == "__main__":
    logger = set_logging()

    results = asyncio.run(
        check_experiment_api(experiment_name="DAPI_GFP_001", configfile=config_path)
    )
    logger.info(results)

    if open_czi:
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
