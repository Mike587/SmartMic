# -*- coding: utf-8 -*-

#################################################################
# File        : MS_versions.py
# Author      : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#
# Copyright(c) 2026 ETH Zurich (ScopeM). All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

"""The identity of the code in use, for the log and for the record of a run.

The gRPC API has no version service (DEV_NOTES "ZEN version is NOT available
over the API"). Thus this module reads each version from a different source:
the folder of the ``zen_api`` stubs, the gateway program, and git. The version
of the ZEN application comes from the ZEN log, in
``MS_zen_logs.zen_app_version``.

Each function is read-only. A source that the function cannot read gives
``None``, and raises no exception. The paths come from ``zeiss_paths`` at the
time of the call. ``zeiss_paths.<name>`` also finds each name in this module.
"""

import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import zeiss_paths


def zen_api_version() -> Tuple[Optional[str], Optional[str]]:
    """Get the ``(version, path)`` of the ``zen_api`` stubs in use, for the log.

    The version comes only from a ``zen_api-<version>`` folder name. The OLD
    layout has no such folder, thus the version is ``None``. ``path`` is
    ``None`` if Python cannot import ``zen_api``. Write ``path`` to the log
    too, because a loose copy can hide a package that has a version.
    """
    try:
        import zen_api
        pkg_dir = Path(zen_api.__file__).resolve().parent
    except Exception:
        return None, None
    return _zen_api_version_from_dir(pkg_dir), str(pkg_dir)


def _zen_api_version_from_dir(pkg_dir: Union[str, Path]) -> Optional[str]:
    """Get the version from a ``zen_api-<version>`` parent folder of a package folder.

    The function does no file operation. It gives ``None`` if there is no such
    parent folder.
    """
    for parent in Path(pkg_dir).parents:
        if parent.name.startswith("zen_api-"):
            return parent.name[len("zen_api-"):]
    return None


def zen_api_gateway_version() -> Optional[str]:
    """Get the FileVersion of ``ZenApiGateway.exe``, or ``None``, for the log.

    An example of a version is ``"3.6.25262.3"``. The version of the gateway is
    the best indication of the API contract. The function uses ``win32api`` to
    read it.
    """
    try:
        import win32api
    except ImportError:
        return None
    try:
        info = win32api.GetFileVersionInfo(str(zeiss_paths.ZEN_API_GATEWAY_EXE), "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
    except Exception:
        return None
    return (f"{win32api.HIWORD(ms)}.{win32api.LOWORD(ms)}."
            f"{win32api.HIWORD(ls)}.{win32api.LOWORD(ls)}")


def smartmic_version() -> Optional[str]:
    """Get the version of SmartMic for the log.

    The function runs ``git describe --tags --always --dirty`` on
    ``zeiss_paths.THIS_DIR``, not on the working directory. An example of a
    result is ``"3bf63cf-dirty"``. The result is ``None`` if git is not
    available, or if THIS_DIR is not in a repository, for example in a copy
    that someone vendored.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(zeiss_paths.THIS_DIR),
             "describe", "--tags", "--always", "--dirty"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_state(repo: Union[str, Path]) -> Dict[str, Any]:
    """Get the state of the git checkout that holds ``repo``, for a run record.

    Returns:
        A dict with the keys ``root``, ``commit`` and ``dirty_files``.
        ``root`` is the checkout that git reported on. It can be a repository
        that ENCLOSES ``repo``. ``commit`` is the full hash. It is ``None`` if
        there is no git, no repository or no commit. ``dirty_files`` holds the
        ``git status --porcelain`` paths, relative to ``root``. It is ``[]``
        if the checkout is clean, or if the function cannot read the state.
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
