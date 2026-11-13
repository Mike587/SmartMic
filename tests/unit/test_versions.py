# -*- coding: utf-8 -*-
"""Unit tests for MS_versions (the versions that preflight writes to the log).

The gRPC API gives no version, so each one is read from a different source.
The parsing logic is a pure helper, tested against synthetic input. The live
readers are checked only for their return-value contract (skipped where the
machine does not have the resource).
"""
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

import zeiss_paths as zp  # noqa: E402  (extends sys.path as a side effect)
import MS_versions as ver  # noqa: E402


def test_parse_version_new_layout_posix():
    p = PurePosixPath("/opt/ZEN-API/python_package/zen_api-2025.10.1/src/zen_api")
    assert ver._zen_api_version_from_dir(p) == "2025.10.1"


def test_parse_version_new_layout_windows():
    p = PureWindowsPath(r"C:\ZEN-API\python_package\zen_api-2026.5.0\src\zen_api")
    assert ver._zen_api_version_from_dir(p) == "2026.5.0"


def test_parse_version_old_loose_layout_is_none():
    p = PurePosixPath("/opt/ZEN-API/python_examples/zen_api")
    assert ver._zen_api_version_from_dir(p) is None


def test_zen_api_version_contract():
    pytest.importorskip("zen_api", reason="zen_api not resolvable on this machine")
    version, path = ver.zen_api_version()
    # version is the parsed folder version or None (loose layout); path always set
    # to the resolved package dir when zen_api imports.
    assert version is None or isinstance(version, str)
    assert isinstance(path, str) and "zen_api" in path


def test_zen_api_gateway_version_missing_exe_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(zp, "ZEN_API_GATEWAY_EXE", tmp_path / "ZenApiGateway.exe")
    assert ver.zen_api_gateway_version() is None


def test_zen_api_gateway_version_contract():
    if not Path(zp.ZEN_API_GATEWAY_EXE).is_file():
        pytest.skip("ZenApiGateway.exe not present on this machine")
    version = ver.zen_api_gateway_version()
    assert version is None or isinstance(version, str)


def test_smartmic_version_none_outside_a_git_repo(tmp_path, monkeypatch):
    # THIS_DIR pointed at a bare tmp dir -> "git describe" fails (not a repo) ->
    # None, same fallback as a project that vendored/copied the modules per the
    # freezing workflow instead of importing them live from a checkout.
    monkeypatch.setattr(zp, "THIS_DIR", tmp_path)
    assert ver.smartmic_version() is None


def test_smartmic_version_none_when_git_missing(monkeypatch):
    import subprocess as sp

    def _raise(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(sp, "run", _raise)
    assert ver.smartmic_version() is None


def test_smartmic_version_contract():
    version = ver.smartmic_version()
    # This repo IS a git checkout, so we expect a real describe string back --
    # at minimum an abbreviated commit hash (no tags exist yet), optionally
    # suffixed "-dirty" for uncommitted changes.
    assert isinstance(version, str) and len(version) > 0


# --------------------------------------------------------------------------
# git_state (full commit + uncommitted files, for run_info records)
# --------------------------------------------------------------------------
def test_git_state_clean_repo(git_repo):
    repo, commit = git_repo
    state = ver.git_state(repo)
    assert state == {"root": str(repo), "commit": commit, "dirty_files": []}
    assert len(state["commit"]) == 40          # the FULL hash, not an abbreviation


def test_git_state_lists_modified_and_untracked(git_repo):
    repo, commit = git_repo
    (repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    state = ver.git_state(repo)
    assert state["commit"] == commit
    assert sorted(state["dirty_files"]) == ["new.txt", "tracked.txt"]


def test_git_state_subfolder_reports_the_enclosing_repo(git_repo):
    # A folder that is not its own repo reports the repo it sits in; "root"
    # shows that (e.g. a project folder without its own .git).
    repo, commit = git_repo
    sub = repo / "sub"
    sub.mkdir()
    state = ver.git_state(sub)
    assert state["root"] == str(repo) and state["commit"] == commit


@pytest.mark.parametrize("make", [lambda p: p, lambda p: p / "missing"])
def test_git_state_not_a_repo(tmp_path, make):
    assert ver.git_state(make(tmp_path)) == {"root": None, "commit": None, "dirty_files": []}


def test_git_state_git_missing(monkeypatch, tmp_path):
    import subprocess as sp

    def _raise(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(sp, "run", _raise)
    assert ver.git_state(tmp_path) == {"root": None, "commit": None, "dirty_files": []}


def test_git_state_on_smartmic_itself():
    state = ver.git_state(zp.THIS_DIR)
    assert state["root"] is not None and len(state["commit"]) == 40
