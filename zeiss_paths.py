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

"""SmartMic bootstrap: puts this repo and the Zeiss ``zen_api`` tree on ``sys.path``.

Import this FIRST, before any MS_* / zen_api import (otherwise ``zen_api``
fails or a test silently skips)::

    import zeiss_paths  # noqa: F401  (side effect: extends sys.path)

Also: ``CONFIG_PATH``, and the identity of the code in use (``zen_api_version``,
``zen_api_gateway_version``, ``smartmic_version``, ``git_state``).
``zeiss_paths.<name>`` also resolves the ``MS_zen_logs`` readers.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

# The ZEN-API python_examples folder (OLD layout: zen_api loose here).
ZEISS_EXAMPLES = Path(
    os.environ.get(
        "SMARTMIC_ZEISS_EXAMPLES",
        r"C:/Users/zeiss/Zeiss_OAD/OAD/ZEN-API/python_examples",
    )
)

THIS_DIR = Path(__file__).resolve().parent

# The one config.ini path (absolute, repo root) for every MS_zenapi_* module.
CONFIG_PATH = THIS_DIR / "config.ini"

# ZEN logs and the gateway exe, for facts the API does not expose.
ZEN_LOGGING_DIR = Path(
    os.environ.get("SMARTMIC_ZEN_LOGGING_DIR", r"C:/ProgramData/Carl Zeiss/Logging")
)
ZEN_API_GATEWAY_EXE = Path(
    os.environ.get(
        "SMARTMIC_ZEN_API_GATEWAY_EXE",
        r"C:/Program Files/Carl Zeiss/ZenApiGateway/ZenApiGateway.exe",
    )
)

# Order matters: THIS_DIR at the FRONT, ZEISS_EXAMPLES at the BACK, so old
# MS_* copies in the Zeiss folder never shadow this repo's.
_this = str(THIS_DIR)
if _this in sys.path:
    sys.path.remove(_this)
sys.path.insert(0, _this)

_zeiss = str(ZEISS_EXAMPLES)
if _zeiss not in sys.path:
    sys.path.append(_zeiss)

def _zen_api_src_version_key(src_path: Union[str, Path]) -> Tuple[int, ...]:
    """Numeric (not lexical: 2025.10 > 2025.9) sort key for ``.../zen_api-<version>/src``.

    An unparsable name gives ``(-1,)``, so it never wins "newest".
    """
    version = src_path.parent.name[len("zen_api-"):]
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (-1,)


# Folder of the versioned packages (ZEN-API/<folder>/zen_api-<version>/src/);
# the name depends on the OAD clone, so both are searched.
ZEN_API_PACKAGE_DIRS = ("python_package", "python_packages")


def _find_zen_api_srcs(examples_dir: Union[str, Path]) -> list:
    """All ``<ZEN_API_PACKAGE_DIRS>/zen_api-<version>/src`` next to ``python_examples``, newest last."""
    root = Path(examples_dir).parent
    found = []
    for name in ZEN_API_PACKAGE_DIRS:
        found.extend((root / name).glob("zen_api-*/src"))
    return sorted(found, key=_zen_api_src_version_key)


# NEW layout: add the newest zen_api-<version>/src, so no pip install is needed.
_zen_api_srcs = _find_zen_api_srcs(ZEISS_EXAMPLES)
# The newest may be ahead of the gateway: pin with SMARTMIC_ZEN_API_VERSION.
_pin = os.environ.get("SMARTMIC_ZEN_API_VERSION")
if _pin:
    _pinned = [s for s in _zen_api_srcs if s.parent.name == f"zen_api-{_pin}"]
    _zen_api_srcs = _pinned or _zen_api_srcs
if _zen_api_srcs:
    _src = str(_zen_api_srcs[-1])  # numeric version sort (see _zen_api_src_version_key); newest is last
    if _src not in sys.path:
        sys.path.append(_src)

# Warn only if zen_api isn't resolvable from ANY location we added.
_zen_api_locations = [ZEISS_EXAMPLES, *_zen_api_srcs]
if not any((Path(p) / "zen_api").is_dir() for p in _zen_api_locations):
    sys.stderr.write(
        f"[zeiss_paths] WARNING: zen_api not found under {ZEISS_EXAMPLES} or "
        f"{ZEISS_EXAMPLES.parent}/{{{','.join(ZEN_API_PACKAGE_DIRS)}}}/zen_api-*/src. "
        f"Set SMARTMIC_ZEISS_EXAMPLES to the correct python_examples path.\n"
    )


def zen_api_version() -> Tuple[Optional[str], Optional[str]]:
    """``(version, path)`` of the resolved ``zen_api`` stubs, for logging.

    The version comes only from a ``zen_api-<version>`` folder name (None in
    the OLD loose layout); ``path`` is None if unimportable. Log ``path`` too: a
    loose copy can shadow a versioned package.
    """
    try:
        import zen_api
        pkg_dir = Path(zen_api.__file__).resolve().parent
    except Exception:
        return None, None
    return _zen_api_version_from_dir(pkg_dir), str(pkg_dir)


def _zen_api_version_from_dir(pkg_dir: Union[str, Path]) -> Optional[str]:
    """Version from a ``zen_api-<version>`` ancestor of a package dir, or None. No I/O."""
    for parent in Path(pkg_dir).parents:
        if parent.name.startswith("zen_api-"):
            return parent.name[len("zen_api-"):]
    return None


def zen_api_gateway_version() -> Optional[str]:
    """FileVersion of ``ZenApiGateway.exe`` (e.g. ``"3.6.25262.3"``), or None, for logging.

    The gateway version tracks the API contract best. Read via ``win32api``.
    """
    try:
        import win32api
    except ImportError:
        return None
    try:
        info = win32api.GetFileVersionInfo(str(ZEN_API_GATEWAY_EXE), "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
    except Exception:
        return None
    return f"{win32api.HIWORD(ms)}.{win32api.LOWORD(ms)}.{win32api.HIWORD(ls)}.{win32api.LOWORD(ls)}"


def smartmic_version() -> Optional[str]:
    """``git describe --tags --always --dirty`` of SmartMic (THIS_DIR, not the CWD), for logging.

    E.g. ``"3bf63cf-dirty"``; None if git is unavailable or THIS_DIR is not in
    a repo (a vendored copy).
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(THIS_DIR), "describe", "--tags", "--always", "--dirty"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_state(repo: Union[str, Path]) -> Dict[str, Any]:
    """Code state of the git checkout containing ``repo``, for run records.

    Returns ``{"root", "commit", "dirty_files"}``: ``root`` is the checkout git
    reported on (can be an ENCLOSING repo); ``commit`` the full hash or None
    (no git / no repo / no commit); ``dirty_files`` the ``git status
    --porcelain`` paths relative to ``root`` (``[]`` if clean or unknown).
    """
    def git(*args: str) -> Optional[str]:
        try:
            r = subprocess.run(["git", "-C", str(repo), *args],
                               capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout if r.returncode == 0 else None

    root = git("rev-parse", "--show-toplevel")
    commit = git("rev-parse", "HEAD") if root else None
    status = (git("status", "--porcelain") or "") if root else ""
    return {"root": str(Path(root.strip())) if root else None,
            "commit": commit.strip() if commit else None,
            "dirty_files": [line[3:] for line in status.splitlines() if line.strip()]}


def __getattr__(name: str) -> Any:
    """Resolve ``MS_zen_logs`` names on this module (lazy: it imports this module)."""
    if name.startswith("__"):
        raise AttributeError(name)
    import MS_zen_logs
    try:
        return getattr(MS_zen_logs, name)
    except AttributeError:
        raise AttributeError(f"module 'zeiss_paths' has no attribute {name!r}") from None
