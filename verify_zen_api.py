# -*- coding: utf-8 -*-

#################################################################
# File        : verify_zen_api.py
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
Test SmartMic against the ``zen_api`` that Python finds.

Run this script AFTER you update the Zeiss tree, or after you set
SMARTMIC_ZEISS_EXAMPLES to a new clone. It shows that nothing is broken BEFORE
you use the microscope::

    pixi run -e smartmic python verify_zen_api.py

The script does these checks. All of them are read-only, thus it is safe to run
the script at any time.

  1. Import each MS_* module. This finds a `zen_api` import that someone
     renamed or moved. It also finds a stub signature that changed below the
     helper functions of SmartMic.
  2. Send some read-only queries. This shows that the stubs can still speak to
     the gateway, and that the messages and the methods still agree.

Exit code 0 means that all the checks passed. Exit code 1 means that one check
or more failed.
"""

import sys
from pathlib import Path
from typing import Any, Callable

# This file is at the root of the SmartMic repository.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import zeiss_paths  # noqa: F401 — extends sys.path so zen_api resolves

_ok = True


def check(label: str, fn: Callable[[], Any]) -> None:
    global _ok
    try:
        result = fn()
        print(f"  [PASS] {label}: {result}")
    except Exception as e:
        _ok = False
        print(f"  [FAIL] {label}: {type(e).__name__}: {e}")


# --- 1. The import test -----------------------------------------------------
print("== import smoke test ==")


def _import_all() -> str:
    # MS_CD7_API_LoA imports each MS_zenapi_* wrapper and the helper
    # functions, including the copied MS_zenapi_helpers. Thus this test uses
    # all the zen_api imports.
    import MS_CD7_API_LoA            # noqa: F401
    import MS_image_analysis         # noqa: F401
    import MS_czexp_editor           # noqa: F401
    import MS_zenapi_helpers         # noqa: F401
    import MS_limits                 # noqa: F401
    import MS_zen_logs               # noqa: F401
    import zen_api                   # noqa: F401
    return "all SmartMic + zen_api modules imported"


check("imports", _import_all)

# Print the tree that Python found. This shows what the script tests.
try:
    import zen_api
    print(f"  zen_api : {Path(zen_api.__file__).parent}")
except Exception as e:
    print(f"  (could not resolve zen_api path: {e})")

if not _ok:
    print("\nImports failed — fix import paths before any live test. Aborting.")
    sys.exit(1)

# --- 2. The read-only queries -----------------------------------------------
print("\n== read-only live queries (gateway must be running) ==")
import MS_CD7_API_LoA as ms  # noqa: E402 — only after the import check above passed

check("sample carrier name", ms.get_sample_carrier_name)
check("objective / optovar", ms.get_current_objective_and_optovar_names)
check("stage XY (m)",        ms.get_current_xy_stage_position)
check("Z position (m)",      ms.get_current_z_position)
check("microscope busy?",    ms.is_microscope_busy)

print("\nRESULT:", "ALL GREEN" if _ok else "FAILURES — see [FAIL] lines above")
sys.exit(0 if _ok else 1)
