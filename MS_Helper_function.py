# -*- coding: utf-8 -*-

#################################################################
# File        : MS_Helper_function.py
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
Compatibility module: the helper functions under their old home.

The code is in three modules, by subject:

* ``MS_run_log``       -- run log, version record, JSONL records, output folders
* ``MS_metadata``      -- positions, sample carrier, focus / stage position from
                          ``.czexp`` and ``.czi`` files
* ``MS_image_metrics`` -- focus score, signal level

Every public name is re-exported here, so ``import MS_Helper_function as
helper`` works as before. New code can import from the module that owns the
function. A value you assign to a name HERE does not reach the owning module:
set it there (e.g. ``MS_run_log.DEFAULT_EXPERIMENT_OUTPUT_FOLDER``).
"""

from MS_run_log import (  # noqa: F401
    DEFAULT_EXPERIMENT_OUTPUT_FOLDER,
    DEFAULT_LOG_FOLDER,
    append_jsonl,
    read_jsonl,
    setup_run_logger,
    write_run_info,
)
from MS_metadata import (  # noqa: F401
    check_sample_carrier,
    get_focus_position_from_czi,
    get_sample_carrier_from_czexp,
    get_sample_carrier_from_czi,
    get_stage_position_from_czi,
    get_xy_position_from_czi,
    get_zstack_z_range,
    load_positions_from_czexp,
    strip_czsht,
)
from MS_image_metrics import (  # noqa: F401
    EMPTY_STACK_SIGNAL_FRAC,
    compute_focus_score,
    is_czi_effectively_empty,
    signal_level_from_czi,
)
