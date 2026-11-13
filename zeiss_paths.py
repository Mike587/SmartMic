# -*- coding: utf-8 -*-

#################################################################
# File        : zeiss_paths.py
# Author      : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#
# Copyright(c) 2026 ETH Zurich (ScopeM). All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

"""The start-up module of SmartMic.

This module puts this repository and the Zeiss ``zen_api`` tree on
``sys.path``. Import it FIRST, before each import of an ``MS_*`` module or of
``zen_api``. If you do not, ``zen_api`` fails, or a test skips and gives no
message::

    import zeiss_paths  # noqa: F401  (the side effect extends sys.path)

The module also gives the paths of this machine: ``CONFIG_PATH``,
``ZEN_LOGGING_DIR`` and ``ZEN_API_GATEWAY_EXE``. ``zeiss_paths.<name>`` also
finds the names in ``MS_versions`` and in ``MS_zen_logs``, which were in this
module before.
"""

import os
import sys
from pathlib import Path
from typing import Any, List, Tuple, Union

# The ZEN-API python_examples folder. In the OLD layout, zen_api is directly
# in this folder.
ZEISS_EXAMPLES = Path(
    os.environ.get(
        "SMARTMIC_ZEISS_EXAMPLES",
        r"C:/Users/zeiss/Zeiss_OAD/OAD/ZEN-API/python_examples",
    )
)

THIS_DIR = Path(__file__).resolve().parent

# The one config.ini path for each MS_zenapi_* module. It is an absolute path
# at the root of the repository.
CONFIG_PATH = THIS_DIR / "config.ini"

# The ZEN logs and the gateway program, for the data that the API does not
# give.
ZEN_LOGGING_DIR = Path(
    os.environ.get("SMARTMIC_ZEN_LOGGING_DIR", r"C:/ProgramData/Carl Zeiss/Logging")
)
ZEN_API_GATEWAY_EXE = Path(
    os.environ.get(
        "SMARTMIC_ZEN_API_GATEWAY_EXE",
        r"C:/Program Files/Carl Zeiss/ZenApiGateway/ZenApiGateway.exe",
    )
)

# The order is important. THIS_DIR goes at the FRONT, and ZEISS_EXAMPLES at
# the BACK. Thus an old copy of an MS_* file in the Zeiss folder never hides
# the file in this repository.
_this = str(THIS_DIR)
if _this in sys.path:
    sys.path.remove(_this)
sys.path.insert(0, _this)

_zeiss = str(ZEISS_EXAMPLES)
if _zeiss not in sys.path:
    sys.path.append(_zeiss)


def _zen_api_src_version_key(src_path: Path) -> Tuple[int, ...]:
    """Make a sort key for a ``.../zen_api-<version>/src`` path.

    The key is numeric, not alphabetic. Thus 2025.10 is above 2025.9. A name
    that the function cannot read gives ``(-1,)``. Thus such a name is never
    the newest.
    """
    version = src_path.parent.name[len("zen_api-"):]
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (-1,)


# The folder of the packages that have a version, at
# ZEN-API/<folder>/zen_api-<version>/src/. The name of the folder depends on
# the OAD clone. Thus the code searches both names.
ZEN_API_PACKAGE_DIRS = ("python_package", "python_packages")


def _find_zen_api_srcs(examples_dir: Union[str, Path]) -> List[Path]:
    """Find each ``zen_api-<version>/src`` folder next to ``python_examples``.

    The function looks in each folder in ``ZEN_API_PACKAGE_DIRS``. The newest
    folder is last in the result.
    """
    root = Path(examples_dir).parent
    found = []
    for name in ZEN_API_PACKAGE_DIRS:
        found.extend((root / name).glob("zen_api-*/src"))
    return sorted(found, key=_zen_api_src_version_key)


# In the NEW layout, add the newest zen_api-<version>/src. Then you do not
# need a pip install.
_zen_api_srcs = _find_zen_api_srcs(ZEISS_EXAMPLES)
# The newest version can be more recent than the gateway. To select one
# version, set SMARTMIC_ZEN_API_VERSION.
_pin = os.environ.get("SMARTMIC_ZEN_API_VERSION")
if _pin:
    _pinned = [s for s in _zen_api_srcs if s.parent.name == f"zen_api-{_pin}"]
    _zen_api_srcs = _pinned or _zen_api_srcs
if _zen_api_srcs:
    # The sort is numeric (see _zen_api_src_version_key), thus the newest is last.
    _src = str(_zen_api_srcs[-1])
    if _src not in sys.path:
        sys.path.append(_src)

# Give a warning only if zen_api is in NONE of the locations that we added.
_zen_api_locations = [ZEISS_EXAMPLES, *_zen_api_srcs]
if not any((Path(p) / "zen_api").is_dir() for p in _zen_api_locations):
    sys.stderr.write(
        f"[zeiss_paths] WARNING: zen_api not found under {ZEISS_EXAMPLES} or "
        f"{ZEISS_EXAMPLES.parent}/{{{','.join(ZEN_API_PACKAGE_DIRS)}}}/zen_api-*/src. "
        f"Set SMARTMIC_ZEISS_EXAMPLES to the correct python_examples path.\n"
    )


def __getattr__(name: str) -> Any:
    """Find a name of ``MS_versions`` or of ``MS_zen_logs`` on this module.

    The imports are in the function, because both modules import this module.
    """
    if name.startswith("__"):
        raise AttributeError(name)
    import MS_versions
    import MS_zen_logs
    for module in (MS_versions, MS_zen_logs):
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module 'zeiss_paths' has no attribute {name!r}")
