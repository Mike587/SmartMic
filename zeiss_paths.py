"""
zeiss_paths.py

Standalone SmartMic project bootstrap.

The MS_* modules depend on the Zeiss-provided ``zen_api`` package (the
auto-generated gRPC stubs), which lives in the Zeiss ZEN-API example/package
tree, not in this repository.  Importing this module inserts that tree (and this
project's own directory) onto ``sys.path`` so ``import zen_api`` resolves no
matter where a script is launched from.

(The ``zen_api_utils`` example glue is no longer a dependency — the helpers
SmartMic used are vendored in ``MS_zenapi_helpers.py``.)

Usage — import this FIRST, before any MS_* / zen_api import::

    import zeiss_paths  # noqa: F401  (side effect: extends sys.path)

If the Zeiss example folder ever moves, update ZEISS_EXAMPLES below (or set the
SMARTMIC_ZEISS_EXAMPLES environment variable to override it).
"""

import os
import sys
from pathlib import Path

# Location of the Zeiss ZEN-API python_examples folder (used to locate zen_api;
# in the OLD layout zen_api shipped loose here).  Override with the
# SMARTMIC_ZEISS_EXAMPLES env var if needed.
ZEISS_EXAMPLES = Path(
    os.environ.get(
        "SMARTMIC_ZEISS_EXAMPLES",
        r"C:/Users/zeiss/Zeiss_OAD/OAD/ZEN-API/python_examples",
    )
)

# This project's own directory, so the MS_* modules can import each other.
THIS_DIR = Path(__file__).resolve().parent

# IMPORTANT ordering:
#   * THIS_DIR goes to the FRONT  -> this project's MS_* modules win.
#   * ZEISS_EXAMPLES goes to the BACK -> used only as a fallback for
#     zen_api (which does not exist in this project).
# Otherwise the old MS_* copies still sitting in the Zeiss folder would
# shadow the ones in this repository.
_this = str(THIS_DIR)
if _this in sys.path:
    sys.path.remove(_this)
sys.path.insert(0, _this)

# In the OLD ZEN-API layout, python_examples also held a loose zen_api folder,
# so keep it on the path as a fallback for that layout.
_zeiss = str(ZEISS_EXAMPLES)
if _zeiss not in sys.path:
    sys.path.append(_zeiss)

# NEW ZEN-API layout (>= 2026.05): zen_api is a separate installable package at
# ZEN-API/python_package/zen_api-<version>/src/.  Add the newest one's src so
# `import zen_api` resolves without pip-installing it.  (The OLD layout had
# zen_api loose in python_examples, already covered above.)
_zen_api_srcs = sorted((ZEISS_EXAMPLES.parent / "python_package").glob("zen_api-*/src"))
# The repo can ship MULTIPLE package versions, and the newest may be ahead of
# the gateway.  Pin one with SMARTMIC_ZEN_API_VERSION
# (e.g. "2025.10.1"); otherwise fall back to the newest available.
_pin = os.environ.get("SMARTMIC_ZEN_API_VERSION")
if _pin:
    _pinned = [s for s in _zen_api_srcs if s.parent.name == f"zen_api-{_pin}"]
    _zen_api_srcs = _pinned or _zen_api_srcs
if _zen_api_srcs:
    _src = str(_zen_api_srcs[-1])  # version dirs sort lexically; newest is last
    if _src not in sys.path:
        sys.path.append(_src)

# Warn only if zen_api isn't resolvable from ANY location we added.
_zen_api_locations = [ZEISS_EXAMPLES, *_zen_api_srcs]
if not any((Path(p) / "zen_api").is_dir() for p in _zen_api_locations):
    sys.stderr.write(
        f"[zeiss_paths] WARNING: zen_api not found under {ZEISS_EXAMPLES} or "
        f"{ZEISS_EXAMPLES.parent / 'python_package'}/zen_api-*/src. "
        f"Set SMARTMIC_ZEISS_EXAMPLES to the correct python_examples path.\n"
    )
