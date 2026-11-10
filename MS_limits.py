# -*- coding: utf-8 -*-

#################################################################
# File        : MS_limits.py
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
Range checks for values that go to the hardware: positions and changer numbers.

One module, so that every layer uses the same limits: the sync wrappers
(``MS_CD7_API_LoA``), the async moves (``MS_zenapi_stage_LM``,
``MS_zenapi_focus``) and the ``.czexp`` writers (``MS_czexp_editor``). No ZEN
API and no other SmartMic import: it is safe to import anywhere.

All lengths are METRES. The limits are those of this CD7 (they are part of the
microscope configuration, see TODO.md "Factor out the microscope
configuration"). To change a limit, set it HERE (``MS_limits.Z_POSITION_MAX``):
the names re-exported by ``MS_CD7_API_LoA`` are copies.

Public functions
----------------
validate_objective_number, validate_optovar_number, validate_z_position,
validate_xy_position       -- return ``(ok, message)``; never raise
require_z_position, require_xy_position, require_objective_number,
require_optovar_number     -- raise ``ValueError`` with that message
validate_range             -- the shared check behind all of them
require_valid              -- raise for a failed ``(ok, message)`` result

Every check refuses ``bool``, ``NaN`` and ``±inf``.
"""

import math
from typing import Any, Tuple, Type, Union

OBJECTIVE_MIN = 1
OBJECTIVE_MAX = 4
OPTOVAR_MIN = 1
OPTOVAR_MAX = 3
Z_POSITION_MIN = -0.01  # metres
Z_POSITION_MAX = 0.01   # metres
X_STAGE_MIN = 0.0       # metres
X_STAGE_MAX = 0.12      # metres
Y_STAGE_MIN = 0.0       # metres
Y_STAGE_MAX = 0.09      # metres


def validate_range(value: Any, lo: float, hi: float, label: str,
                   type_check: Union[Type, Tuple[Type, ...]], type_name: str,
                   unit: str = "") -> Tuple[bool, str]:
    """Shared isinstance + min/max check; returns ``(ok, message)``.

    bool is refused (it is an int in Python: True would select position 1).
    NaN and ±inf are refused: every comparison with NaN is False, so a plain
    ``value < lo or value > hi`` test lets NaN through to the hardware.
    """
    if isinstance(value, bool) or not isinstance(value, type_check):
        return False, f"{label} must be {type_name}, got {type(value)}"
    if not math.isfinite(value):
        return False, f"{label} must be a finite number, got {value}"
    if not lo <= value <= hi:
        return False, f"{label} {value}{unit} outside safe range [{lo}, {hi}]{unit}"
    return True, f"Valid {label.lower()}"


def require_valid(check: Tuple[bool, str], what: str) -> None:
    """Raise ValueError for a failed validate_* result (its (ok, message) tuple)."""
    ok, message = check
    if not ok:
        raise ValueError(f"Invalid {what}: {message}")


def validate_objective_number(objective_nr: int) -> Tuple[bool, str]:
    """Check that an objective position number is in the valid range.

    Returns:
        (is_valid, message)
    """
    return validate_range(objective_nr, OBJECTIVE_MIN, OBJECTIVE_MAX,
                          "Objective number", int, "integer")


def validate_optovar_number(optovar_nr: int) -> Tuple[bool, str]:
    """Check that an optovar position number is in the valid range.

    Returns:
        (is_valid, message)
    """
    return validate_range(optovar_nr, OPTOVAR_MIN, OPTOVAR_MAX,
                          "Optovar number", int, "integer")


def validate_z_position(z_pos: float) -> Tuple[bool, str]:
    """Check that a Z position (metres) is within the limits.

    Returns:
        (is_valid, message)
    """
    return validate_range(z_pos, Z_POSITION_MIN, Z_POSITION_MAX,
                          "Z position", (int, float), "numeric", unit="m")


def validate_xy_position(x: float, y: float) -> Tuple[bool, str]:
    """Check that an XY position (metres) is within the stage limits.

    Returns:
        (is_valid, message)
    """
    x_ok, x_msg = validate_range(x, X_STAGE_MIN, X_STAGE_MAX,
                                 "X coordinate", (int, float), "numeric", unit="m")
    if not x_ok:
        return False, x_msg
    y_ok, y_msg = validate_range(y, Y_STAGE_MIN, Y_STAGE_MAX,
                                 "Y coordinate", (int, float), "numeric", unit="m")
    if not y_ok:
        return False, y_msg
    return True, "Valid XY coordinates within stage limits"


def require_objective_number(objective_nr: int) -> None:
    """Raise ValueError unless ``objective_nr`` is a valid objective position."""
    require_valid(validate_objective_number(objective_nr), "objective number")


def require_optovar_number(optovar_nr: int) -> None:
    """Raise ValueError unless ``optovar_nr`` is a valid optovar position."""
    require_valid(validate_optovar_number(optovar_nr), "optovar number")


def require_z_position(z_m: float) -> None:
    """Raise ValueError unless ``z_m`` (metres) is a finite Z within the limits."""
    require_valid(validate_z_position(z_m), "Z position")


def require_xy_position(x_m: float, y_m: float) -> None:
    """Raise ValueError unless ``x_m`` / ``y_m`` (metres) are within the stage limits."""
    require_valid(validate_xy_position(x_m, y_m), "XY coordinates")
