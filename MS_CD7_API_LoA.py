# -*- coding: utf-8 -*-

#################################################################
# File        : MS_CD7_API_LoA.py
# Author      : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#
# Copyright(c) 2026 ETH Zurich (ScopeM). All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

"""
The synchronous entry point of the SmartMic library.

Use it as ``import MS_CD7_API_LoA as ms``. Each function puts the asynchronous
``MS_zenapi_*`` calls into ``asyncio.run``. It also adds the checks and the
guards that the raw calls do not have.

The code
--------
This module holds no code of its own. It imports each public name from the
module that has it:

    MS_CD7_devices      One device or one call: the moves, the focus searches,
                        the optics change and the read functions.
    MS_definite_focus   DefiniteFocusTracker, and the checks of the carrier
                        bottom that DF uses.
    MS_CD7_procedures   The procedures that use more than one device: the
                        preflight and the tray.
    MS_limits           The limits and the range checks.

A name on this module is a second name for the same object. To replace a
function in a test, replace it in the module that has it. A replacement on
this module has no effect on the code in the other modules.

Rules
-----
* Each function is synchronous. It makes its own event loop and its own gRPC
  channel for each call. Do not call these functions from asynchronous code.
* Each length is in METRES. The exception is a name or a key that says
  something different, for example ``thickness_um``.
* An argument that is not correct raises ``ValueError`` or ``TypeError``
  before any hardware call. The range checks are in ``MS_limits``.
* A call that gets no answer raises ``TimeoutError``. The limit is 60 seconds
  for a read and 300 seconds for a move. An acquisition has no limit. See
  ``MS_zenapi_helpers``.
* A failure RAISES an exception (DEV_NOTES "Rule: a failure raises"). There is
  one exception to this rule. ``DefiniteFocusTracker.find_surface`` returns
  ``None`` if one position fails, and the pipeline then goes to the next
  position. It raises an exception after more failures.

The public functions
--------------------
The start of a run
    preflight                       Check the carrier, the busy state, the
                                    stage motion and the versions. It raises
                                    PreflightFailed.
    is_microscope_busy              True while an experiment or an acquisition
                                    runs.
    get_running_experiment_status   The status of that experiment, or None.
    stop_experiment                 Stop it, for a crash handler or a Ctrl-C
                                    handler.

Focus: the Z-drive, DefiniteFocus and the software autofocus
    get_current_z_position          The Z in metres.
    move_focus_to_new_z_position    An absolute Z move with a range check.
    df_find_surface                 DF FindSurface with retries. It gives the
                                    Z in metres.
    df_recall_focus                 DF RecallFocus. It gives the Z in metres.
    DefiniteFocusTracker            FindSurface with a start point that
                                    adapts, and a fault after more failures.
    swaf_find_focus                 The software autofocus from a .czexp file.
                                    It gives the Z in metres.

The stage
    get_current_xy_stage_position   [x, y] in metres.
    move_stage_to_new_xy_position   It lowers Z first. It refuses the move
                                    while the immersion objective is active.
    set_stage_motion_sync           The stage speed and acceleration, in
                                    percent.

The optics
    get_current_objective_and_optovar        The position indices.
    get_current_objective_and_optovar_names  The names and the positions.
    immersion_objective_active      True if the active objective is an
                                    immersion objective.
    check_immersion_guard           Compare the immersion types of the API
                                    with the known slot.
    set_optics_verified             Change the optics, then wait for a stable
                                    read-back.

The sample carrier and the tray
    get_sample_carrier_info         All the data of the carrier.
    get_sample_carrier_name         The name of the carrier.
    carrier_bottom                  The name, the material and the thickness
                                    of the bottom, as a CarrierBottom dict.
    check_carrier_for_df            Compare the bottom that DF uses with the
                                    value from the API.
    wait_for_df_bottom              Wait until DF has the bottom after a load.
    get_current_container           The slide position in an insert.
    move_to_container               Move to a slide position in an insert.
    get_live_scan_configuration     What the prescan does at a tray load.
    eject_tray                      A tray move with guards.
    load_tray_and_prescan           A tray move with guards.

The acquisition
    run_experiment_from_path        Run a ``.czexp`` file.

The checks. Each one returns ``(ok, message)``, and the moves call them.
    validate_objective_number, validate_optovar_number, validate_z_position,
    validate_xy_position

The exceptions
    FocusSearchFailed               One DF search or SWAF search failed.
    PreflightFailed                 A preflight check failed.
    DefiniteFocusFault              DF failed more than one time. Stop the run.
    CarrierChanged                  This is a DefiniteFocusFault.
    ImmersionObjectiveActive        The code refused a move that lowers Z to
                                    0, because the immersion objective is
                                    active.

For the behaviour of the hardware, and for the reasons for the guards, see
DEV_NOTES.md.
"""

import zeiss_paths  # noqa: F401  — import FIRST (side effect: extends sys.path for zen_api)
from MS_limits import (
    OBJECTIVE_MIN, OBJECTIVE_MAX, OPTOVAR_MIN, OPTOVAR_MAX, Z_POSITION_MIN,
    Z_POSITION_MAX, X_STAGE_MIN, X_STAGE_MAX, Y_STAGE_MIN, Y_STAGE_MAX,
    validate_objective_number, validate_optovar_number, validate_z_position,
    validate_xy_position,
)
from MS_CD7_devices import (
    FocusSearchFailed, ImmersionObjectiveActive, is_microscope_busy,
    get_running_experiment_status, stop_experiment, get_current_z_position,
    move_focus_to_new_z_position, df_find_surface, df_recall_focus, swaf_find_focus,
    get_current_xy_stage_position, move_stage_to_new_xy_position,
    set_stage_motion_sync, get_current_objective_and_optovar,
    get_current_objective_and_optovar_names, immersion_objective_active,
    set_optics_verified, get_sample_carrier_info, get_sample_carrier_name,
    get_current_container, move_to_container, get_live_scan_configuration,
    run_experiment_from_path,
)
from MS_definite_focus import (
    CarrierBottom, CarrierChanged, DefiniteFocusFault, DefiniteFocusTracker,
    carrier_bottom, check_carrier_for_df, wait_for_df_bottom,
)
from MS_CD7_procedures import (
    PreflightFailed, preflight, check_immersion_guard, eject_tray,
    load_tray_and_prescan,
)

# The public names, by module.
__all__ = [
    # MS_limits
    "OBJECTIVE_MIN", "OBJECTIVE_MAX", "OPTOVAR_MIN", "OPTOVAR_MAX", "Z_POSITION_MIN",
    "Z_POSITION_MAX", "X_STAGE_MIN", "X_STAGE_MAX", "Y_STAGE_MIN", "Y_STAGE_MAX",
    "validate_objective_number", "validate_optovar_number", "validate_z_position",
    "validate_xy_position",
    # MS_CD7_devices
    "FocusSearchFailed", "ImmersionObjectiveActive", "is_microscope_busy",
    "get_running_experiment_status", "stop_experiment", "get_current_z_position",
    "move_focus_to_new_z_position", "df_find_surface", "df_recall_focus",
    "swaf_find_focus", "get_current_xy_stage_position",
    "move_stage_to_new_xy_position", "set_stage_motion_sync",
    "get_current_objective_and_optovar", "get_current_objective_and_optovar_names",
    "immersion_objective_active", "set_optics_verified", "get_sample_carrier_info",
    "get_sample_carrier_name", "get_current_container", "move_to_container",
    "get_live_scan_configuration", "run_experiment_from_path",
    # MS_definite_focus
    "CarrierBottom", "CarrierChanged", "DefiniteFocusFault", "DefiniteFocusTracker",
    "carrier_bottom", "check_carrier_for_df", "wait_for_df_bottom",
    # MS_CD7_procedures
    "PreflightFailed", "preflight", "check_immersion_guard", "eject_tray",
    "load_tray_and_prescan",
]
