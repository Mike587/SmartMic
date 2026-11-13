# -*- coding: utf-8 -*-
"""Unit tests for zeiss_paths (the search for the zen_api package folders)."""
from pathlib import Path

import zeiss_paths as zp  # noqa: E402  (extends sys.path as a side effect)


def _make_pkg(root: Path, folder: str, version: str) -> Path:
    src = root / "ZEN-API" / folder / f"zen_api-{version}" / "src"
    (src / "zen_api").mkdir(parents=True)
    return src


def test_find_srcs_old_folder_name(tmp_path):
    # OAD checkouts before the rename: ZEN-API/python_package/
    src = _make_pkg(tmp_path, "python_package", "2025.10.1")
    assert zp._find_zen_api_srcs(tmp_path / "ZEN-API" / "python_examples") == [src]


def test_find_srcs_new_folder_name(tmp_path):
    # Current OAD layout: ZEN-API/python_packages/
    src = _make_pkg(tmp_path, "python_packages", "2025.10.1")
    assert zp._find_zen_api_srcs(tmp_path / "ZEN-API" / "python_examples") == [src]


def test_find_srcs_both_names_sorted_by_version(tmp_path):
    old = _make_pkg(tmp_path, "python_package", "2026.5.1")
    new = _make_pkg(tmp_path, "python_packages", "2025.10.1")
    newest = _make_pkg(tmp_path, "python_packages", "2026.11.1")
    found = zp._find_zen_api_srcs(tmp_path / "ZEN-API" / "python_examples")
    assert found == [new, old, newest]


def test_find_srcs_none(tmp_path):
    assert zp._find_zen_api_srcs(tmp_path / "ZEN-API" / "python_examples") == []
