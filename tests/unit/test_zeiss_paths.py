# -*- coding: utf-8 -*-
"""Unit tests for zeiss_paths version helpers (all logged at preflight).

None of ZEN app / ZenApiGateway / zen_api stub version is available over the
gRPC API, so each is recovered out-of-band. The parsing logic for each is a
pure, I/O-free helper tested here against synthetic input; the live wrappers
are checked only for their return-value contract (skipped where the machine
doesn't have the resource being read).
"""
from pathlib import Path, PurePosixPath, PureWindowsPath

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


def test_last_software_version_picks_the_last_match():
    # ZEN.log.xml is a flat stream of <event> fragments (no wrapping root), each
    # stamping its own SoftwareVersion -- take the LAST one, not the first, so a
    # version that changed mid-file (a ZEN restart/upgrade) reflects what's
    # running now.
    text = (
        '<event><properties><data name="SoftwareVersion" value="3.12.9.0" /></properties></event>\n'
        '<event><properties><data name="SoftwareVersion" value="3.13.109.08000" /></properties></event>\n'
    )
    assert zp._last_software_version(text) == "3.13.109.08000"


def test_last_software_version_none_when_absent():
    assert zp._last_software_version("<event><message>no version here</message></event>") is None


def test_zen_app_version_missing_file_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(zp, "ZEN_LOGGING_DIR", tmp_path / "does_not_exist")
    assert zp.zen_app_version() is None


def test_zen_app_version_contract():
    log_file = zp.ZEN_LOGGING_DIR / "ZEN.log.xml"
    if not log_file.is_file():
        pytest.skip("ZEN.log.xml not present on this machine")
    version = zp.zen_app_version()
    assert version is None or isinstance(version, str)


def test_zen_api_gateway_version_missing_exe_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(zp, "ZEN_API_GATEWAY_EXE", tmp_path / "ZenApiGateway.exe")
    assert zp.zen_api_gateway_version() is None


def test_zen_api_gateway_version_contract():
    if not Path(zp.ZEN_API_GATEWAY_EXE).is_file():
        pytest.skip("ZenApiGateway.exe not present on this machine")
    version = zp.zen_api_gateway_version()
    assert version is None or isinstance(version, str)


def test_smartmic_version_none_outside_a_git_repo(tmp_path, monkeypatch):
    # THIS_DIR pointed at a bare tmp dir -> "git describe" fails (not a repo) ->
    # None, same fallback as a project that vendored/copied the modules per the
    # freezing workflow instead of importing them live from a checkout.
    monkeypatch.setattr(zp, "THIS_DIR", tmp_path)
    assert zp.smartmic_version() is None


def test_smartmic_version_none_when_git_missing(monkeypatch):
    import subprocess as sp

    def _raise(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(sp, "run", _raise)
    assert zp.smartmic_version() is None


def test_smartmic_version_contract():
    version = zp.smartmic_version()
    # This repo IS a git checkout, so we expect a real describe string back --
    # at minimum an abbreviated commit hash (no tags exist yet), optionally
    # suffixed "-dirty" for uncommitted changes.
    assert isinstance(version, str) and len(version) > 0
