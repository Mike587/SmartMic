"""
verify_zen_api.py — smoke-test SmartMic against the resolved zen_api.

Run this AFTER updating the Zeiss tree (or after pointing SMARTMIC_ZEISS_EXAMPLES
at a new clone) to confirm nothing broke BEFORE touching the microscope:

    pixi run -e smartmic python verify_zen_api.py

Checks (all read-only — safe to run any time):
  1. Import every MS_* module → catches renamed/moved `zen_api` imports or
     stub signatures that changed under SmartMic's vendored helpers.
  2. A few read-only live queries → confirms the stubs still talk to the gateway
     and the message/method shapes still match.

Exit code 0 = all green; 1 = at least one failure.
"""

import sys
from pathlib import Path

# This file lives at the SmartMic repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import zeiss_paths  # noqa: F401 — extends sys.path so zen_api / zen_api_utils resolve

_ok = True


def check(label, fn):
    global _ok
    try:
        result = fn()
        print(f"  [PASS] {label}: {result}")
    except Exception as e:
        _ok = False
        print(f"  [FAIL] {label}: {type(e).__name__}: {e}")


# --- 1. import smoke test ---------------------------------------------------
print("== import smoke test ==")


def _import_all():
    # MS_CD7_API_LoA transitively imports every MS_zenapi_* wrapper + helpers
    # (incl. the vendored MS_zenapi_helpers), so this exercises the whole
    # zen_api import surface.
    import MS_CD7_API_LoA            # noqa: F401
    import MS_image_analysis         # noqa: F401
    import MS_czexp_editor           # noqa: F401
    import MS_zenapi_helpers         # noqa: F401
    import zen_api                   # noqa: F401
    return "all SmartMic + zen_api modules imported"


check("imports", _import_all)

# Show exactly which tree resolved (the definitive "what am I testing").
try:
    import zen_api
    print(f"  zen_api : {Path(zen_api.__file__).parent}")
except Exception as e:
    print(f"  (could not resolve zen_api path: {e})")

if not _ok:
    print("\nImports failed — fix import paths before any live test. Aborting.")
    sys.exit(1)

# --- 2. read-only live queries ----------------------------------------------
print("\n== read-only live queries (gateway must be running) ==")
import MS_CD7_API_LoA as ms

check("sample carrier name", ms.get_sample_carrier_name)
check("objective / optovar", ms.get_current_objective_and_optovar_names)
check("stage XY (m)",        ms.get_current_xy_stage_position)
check("Z position (m)",      ms.get_current_z_position)
check("microscope busy?",    ms.is_microscope_busy)

print("\nRESULT:", "ALL GREEN" if _ok else "FAILURES — see [FAIL] lines above")
sys.exit(0 if _ok else 1)
