"""
zeiss_paths.py

Standalone SmartMic project bootstrap.

The MS_* modules depend on two Zeiss-provided packages — ``zen_api`` and
``zen_api_utils`` — which live in the Zeiss ZEN-API example folder, not in this
repository.  Importing this module inserts that folder (and this project's own
directory) onto ``sys.path`` so those imports resolve no matter where a script
is launched from.

Usage — import this FIRST, before any MS_* / zen_api import::

    import zeiss_paths  # noqa: F401  (side effect: extends sys.path)

If the Zeiss example folder ever moves, update ZEISS_EXAMPLES below (or set the
SMARTMIC_ZEISS_EXAMPLES environment variable to override it).
"""

import os
import sys
from pathlib import Path

# Location of the Zeiss ZEN-API python_examples folder that ships zen_api /
# zen_api_utils.  Override with the SMARTMIC_ZEISS_EXAMPLES env var if needed.
ZEISS_EXAMPLES = Path(
    os.environ.get(
        "SMARTMIC_ZEISS_EXAMPLES",
        r"C:/Users/zeiss/Zeiss_OAD/OAD/ZEN-API/python_examples",
    )
)

# This project's own directory, so the MS_* modules can import each other.
THIS_DIR = Path(__file__).resolve().parent

for _p in (THIS_DIR, ZEISS_EXAMPLES):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

if not (ZEISS_EXAMPLES / "zen_api").is_dir():
    sys.stderr.write(
        f"[zeiss_paths] WARNING: zen_api not found under {ZEISS_EXAMPLES}. "
        f"Set SMARTMIC_ZEISS_EXAMPLES to the correct python_examples path.\n"
    )
