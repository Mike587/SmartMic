# -*- coding: utf-8 -*-
"""Unit tests for zeiss_paths.zen_api_version (logged at preflight).

ZEN exposes no version over the API, so we log the zen_api stub package folder
version when the versioned layout is in use. The version-parsing is a pure
path-only helper, tested here for both layouts; the live wrapper is checked for
its (version, path) contract.
"""
from pathlib import PurePosixPath, PureWindowsPath

import pytest

import zeiss_paths as zp  # noqa: E402  (extends sys.path as a side effect)


def test_parse_version_new_layout_posix():
    p = PurePosixPath("/opt/ZEN-API/python_package/zen_api-2025.10.1/src/zen_api")
    assert zp._zen_api_version_from_dir(p) == "2025.10.1"


def test_parse_version_new_layout_windows():
    p = PureWindowsPath(r"C:\ZEN-API\python_package\zen_api-2026.5.0\src\zen_api")
    assert zp._zen_api_version_from_dir(p) == "2026.5.0"


def test_parse_version_old_loose_layout_is_none():
    p = PurePosixPath("/opt/ZEN-API/python_examples/zen_api")
    assert zp._zen_api_version_from_dir(p) is None


def test_zen_api_version_contract():
    pytest.importorskip("zen_api", reason="zen_api not resolvable on this machine")
    version, path = zp.zen_api_version()
    # version is the parsed folder version or None (loose layout); path always set
    # to the resolved package dir when zen_api imports.
    assert version is None or isinstance(version, str)
    assert isinstance(path, str) and "zen_api" in path
