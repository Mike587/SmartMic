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

The single source of limits for every layer (sync wrappers, async moves,
``.czexp`` writers). No ZEN API and no SmartMic import: safe to import anywhere.

All lengths are METRES; the limits are those of this CD7. Change a limit HERE:
the names re-exported by ``MS_CD7_API_LoA`` are copies.

validate_* return ``(ok, message)`` and never raise; require_* raise
``ValueError``. Every check refuses ``bool``, ``NaN`` and ``±inf``.
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

    Refuses bool (True would select position 1) and NaN/±inf (a plain
    ``value < lo or value > hi`` test lets NaN through to the hardware).
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
    """Check an objective position number; returns ``(ok, message)``."""
    return validate_range(objective_nr, OBJECTIVE_MIN, OBJECTIVE_MAX,
                          "Objective number", int, "integer")


def validate_optovar_number(optovar_nr: int) -> Tuple[bool, str]:
    """Check an optovar position number; returns ``(ok, message)``."""
    return validate_range(optovar_nr, OPTOVAR_MIN, OPTOVAR_MAX,
                          "Optovar number", int, "integer")


def validate_z_position(z_pos: float) -> Tuple[bool, str]:
    """Check a Z position (metres); returns ``(ok, message)``."""
    return validate_range(z_pos, Z_POSITION_MIN, Z_POSITION_MAX,
                          "Z position", (int, float), "numeric", unit="m")


def validate_xy_position(x: float, y: float) -> Tuple[bool, str]:
    """Check an XY position (metres) against the stage limits; returns ``(ok, message)``."""
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
