# -*- coding: utf-8 -*-
"""Unit tests for zeiss_paths version helpers (all logged at preflight).

None of ZEN app / ZenApiGateway / zen_api stub version is available over the
gRPC API, so each is recovered out-of-band. The parsing logic for each is a
pure, I/O-free helper tested here against synthetic input; the live wrappers
are checked only for their return-value contract (skipped where the machine
doesn't have the resource being read).
"""
import os
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


# --------------------------------------------------------------------------
# git_state (full commit + uncommitted files, for run_info records)
# --------------------------------------------------------------------------
def test_git_state_clean_repo(git_repo):
    repo, commit = git_repo
    state = zp.git_state(repo)
    assert state == {"root": str(repo), "commit": commit, "dirty_files": []}
    assert len(state["commit"]) == 40          # the FULL hash, not an abbreviation


def test_git_state_lists_modified_and_untracked(git_repo):
    repo, commit = git_repo
    (repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
    (repo / "new.txt").write_text("x\n", encoding="utf-8")
    state = zp.git_state(repo)
    assert state["commit"] == commit
    assert sorted(state["dirty_files"]) == ["new.txt", "tracked.txt"]


def test_git_state_subfolder_reports_the_enclosing_repo(git_repo):
    # A folder that is not its own repo reports the repo it sits in; "root"
    # shows that (e.g. a project folder without its own .git).
    repo, commit = git_repo
    sub = repo / "sub"
    sub.mkdir()
    state = zp.git_state(sub)
    assert state["root"] == str(repo) and state["commit"] == commit


@pytest.mark.parametrize("make", [lambda p: p, lambda p: p / "missing"])
def test_git_state_not_a_repo(tmp_path, make):
    assert zp.git_state(make(tmp_path)) == {"root": None, "commit": None, "dirty_files": []}


def test_git_state_git_missing(monkeypatch, tmp_path):
    import subprocess as sp

    def _raise(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(sp, "run", _raise)
    assert zp.git_state(tmp_path) == {"root": None, "commit": None, "dirty_files": []}


def test_git_state_on_smartmic_itself():
    state = zp.git_state(zp.THIS_DIR)
    assert state["root"] is not None and len(state["commit"]) == 40


# --------------------------------------------------------------------------
# Auto-immersion (re-immersion) settings: Options.config + ZEN logs
# --------------------------------------------------------------------------
_OPTIONS_CONFIG = """\ufeff<?xml version="1.0" encoding="utf-8"?>
<Options>
  <OptionEntry Name="3D.EnablePrefetching" Value="true" />
  <OptionEntry Name="AutoImmersionFeedPumpDuration" Value="2500" />
  <OptionEntry Name="AutoImmersionRefillInterval" Value="10" />
  <OptionEntry Name="AutoImmersionRefillTime" Value="45" />
  <OptionEntry Name="AutoImmersionRefillTravelDistance" Value="40" />
  <OptionEntry Name="IsAutoImmersionEnabled" Value="true" />
  <OptionEntry Name="IsAutoImmersionRefillGlobalEnabled" Value="true" />
  <OptionEntry Name="IsAutoImmersionRefillTravelDistanceEnabled" Value="false" />
</Options>
"""


def _zen_log_save(path):
    """One ZEN.log.xml event naming an Options.config it wrote."""
    return ('<event logger="X" timestamp="2026-11-09T10:56:04+01:00" level="INFO">'
            "<message>SaveToFile(): Successfully written options config file "
            f"'{path}'.</message></event>\n")


def _service_options_event(timestamp, interval):
    """One ZENService.log.xml SetServiceOptions event (immersion payload if interval)."""
    payload = "    <UseAlignedLedIntensities>false</UseAlignedLedIntensities>\n"
    if interval is not None:
        payload += (
            "    <IsAutoImmersionEnabled>True</IsAutoImmersionEnabled>\n"
            f"    <AutoImmersionRefillInterval>{interval}</AutoImmersionRefillInterval>\n"
            "    <AutoImmersionRefillTravelDistance>200</AutoImmersionRefillTravelDistance>\n"
            "    <AutoImmersionBubbleEliminationMovementDistanceZ>0.5"
            "</AutoImmersionBubbleEliminationMovementDistanceZ>\n"
        )
    return (f'<event logger="Zeiss.Micro.ZENService.GRPC.GrpcZenService" timestamp="{timestamp}" '
            'level="DEBUG" thread="17" domain="ZENService"><message><![CDATA[StreamRequestAsync(): '
            'Receiving ServiceRequest: <ServiceRequest Id="SetServiceOptions" RequestId="4" '
            f'ClientId="PC:">\n  <Payload>\n{payload}  </Payload>\n</ServiceRequest>]]></message>'
            '<properties><data name="SoftwareVersion" value="3.13.109.08000" /></properties></event>\n')


def _touch(path, text, mtime):
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_coerce_option_value():
    assert zp._coerce_option_value("True") is True
    assert zp._coerce_option_value("false") is False
    assert zp._coerce_option_value("120") == 120
    assert zp._coerce_option_value("0.5") == 0.5
    assert zp._coerce_option_value("abc") == "abc"


def test_immersion_options_from_options_config_keeps_only_the_sent_keys():
    opts = zp._immersion_options_from_options_config(_OPTIONS_CONFIG)
    assert opts == {
        "IsAutoImmersionEnabled": True,
        "IsAutoImmersionRefillGlobalEnabled": True,
        "IsAutoImmersionRefillTravelDistanceEnabled": False,
        "AutoImmersionRefillInterval": 10,
        "AutoImmersionRefillTravelDistance": 40,
        "AutoImmersionFeedPumpDuration": 2500,
    }
    # The leftover key that ZEN does not send on is not reported.
    assert "AutoImmersionRefillTime" not in opts


def test_user_options_path_skips_workgroup_and_install_files():
    text = (
        "<event><message>workgroupOptionsFileName = "
        r"'C:\ProgramData\Carl Zeiss\ZEN\Options.config'</message></event>"
        "\n<event><message>userOptionsFileName = "
        r"'C:\ProgramData\Carl Zeiss\ZEN\Users\Pierce\DefaultWorkspace\Options.config'"
        "</message></event>\n"
        + _zen_log_save(r"C:\ProgramData\Carl Zeiss\ZEN\Users\mike\DefaultWorkspace\Options.config")
        + _zen_log_save(r"C:\ProgramData\Carl Zeiss\ZEN\Options.config")
        + _zen_log_save(r"C:\Program Files\Carl Zeiss\ZEN 2\ZEN\Options.config")
    )
    # The LAST user file wins (a later ZEN session / user).
    assert zp._user_options_path_from_zen_log(text) == \
        r"C:\ProgramData\Carl Zeiss\ZEN\Users\mike\DefaultWorkspace\Options.config"


def test_user_options_path_none_when_absent():
    assert zp._user_options_path_from_zen_log(
        _zen_log_save(r"C:\ProgramData\Carl Zeiss\ZEN\Options.config")) is None


def test_last_service_immersion_options_picks_last_payload_with_keys():
    text = (_service_options_event("2026-11-06T08:37:22+01:00", 120)
            + _service_options_event("2026-11-09T10:04:06+01:00", 10)
            # A later SetServiceOptions without immersion keys must not hide it.
            + _service_options_event("2026-11-09T10:05:44+01:00", None))
    opts, ts = zp._last_service_immersion_options(text)
    assert ts == "2026-11-09T10:04:06+01:00"
    assert opts == {"IsAutoImmersionEnabled": True,
                    "AutoImmersionRefillInterval": 10,
                    "AutoImmersionRefillTravelDistance": 200}


def test_last_service_immersion_options_none_when_absent():
    assert zp._last_service_immersion_options(
        _service_options_event("2026-11-09T10:05:44+01:00", None)) == (None, None)


def test_zen_auto_immersion_settings_end_to_end(tmp_path, monkeypatch):
    # A fake ZEN install: the user file under ZEN\Users\<user>, rotated logs.
    logs = tmp_path / "Logging"
    logs.mkdir()
    old_user = tmp_path / "ZEN" / "Users" / "old" / "DefaultWorkspace" / "Options.config"
    user_file = tmp_path / "ZEN" / "Users" / "bob" / "DefaultWorkspace" / "Options.config"
    user_file.parent.mkdir(parents=True)
    user_file.write_text(_OPTIONS_CONFIG, encoding="utf-8")

    # ZEN.log.xml.1 is older (another user); ZEN.log.xml is the current one.
    _touch(logs / "ZEN.log.xml.1", _zen_log_save(old_user), 1000)
    _touch(logs / "ZEN.log.xml", _zen_log_save(user_file), 2000)
    # The startup payload sits in an older rotation; the current file has none.
    _touch(logs / "ZENService.log.xml.3",
           _service_options_event("2026-11-09T10:04:06+01:00", 120), 1500)
    _touch(logs / "ZENService.log.xml", "<event><message>nothing</message></event>\n", 2000)
    # Newest of all, but not a rotation of ZENService.log.xml: must be ignored.
    _touch(logs / "ZENService.Error.log.xml",
           _service_options_event("2020-01-01T00:00:00+01:00", 999), 3000)

    monkeypatch.setattr(zp, "ZEN_LOGGING_DIR", logs)
    info = zp.zen_auto_immersion_settings()

    assert info["user_options_path"] == user_file
    assert info["user_options"]["AutoImmersionRefillInterval"] == 10
    assert info["service_options"]["AutoImmersionRefillInterval"] == 120
    assert info["service_options_time"] == "2026-11-09T10:04:06+01:00"

    line = zp.format_auto_immersion_settings(info)
    assert "ZEN user 'bob'" in line
    assert "every 10 min / distance off" in line
    assert "every 120 min / 200 mm" in line
    assert "MISMATCH" in line


def test_zen_auto_immersion_settings_missing_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(zp, "ZEN_LOGGING_DIR", tmp_path / "does_not_exist")
    info = zp.zen_auto_immersion_settings()
    assert info == {"user_options_path": None, "user_options": None,
                    "service_options": None, "service_options_time": None}
    line = zp.format_auto_immersion_settings(info)
    assert "ZEN user '?'" in line and "unknown" in line and "MISMATCH" not in line


def test_zen_auto_immersion_settings_user_file_gone(tmp_path, monkeypatch):
    # The log names a file that no longer exists: path reported, options None.
    logs = tmp_path / "Logging"
    logs.mkdir()
    gone = tmp_path / "ZEN" / "Users" / "bob" / "DefaultWorkspace" / "Options.config"
    _touch(logs / "ZEN.log.xml", _zen_log_save(gone), 2000)
    monkeypatch.setattr(zp, "ZEN_LOGGING_DIR", logs)
    info = zp.zen_auto_immersion_settings()
    assert info["user_options_path"] == gone
    assert info["user_options"] is None


@pytest.mark.parametrize("path, user", [
    (r"C:\ProgramData\Carl Zeiss\ZEN\Users\mike\DefaultWorkspace\Options.config", "mike"),
    (r"C:\ProgramData\Carl Zeiss\ZEN\Users\mike\Workspaces\MS01\Options.config", "mike"),
    (r"C:\ProgramData\Carl Zeiss\ZEN\DefaultUser\DefaultWorkspace\Options.config", "DefaultUser"),
])
def test_zen_user_from_options_path(path, user):
    assert zp._zen_user_from_options_path(path) == user


@pytest.mark.parametrize("opts, text", [
    (None, "unknown"),
    ({"IsAutoImmersionEnabled": False}, "auto-immersion OFF"),
    ({"IsAutoImmersionRefillGlobalEnabled": False}, "refill OFF"),
    ({"AutoImmersionRefillInterval": 120, "AutoImmersionRefillTravelDistance": 200,
      "IsAutoImmersionRefillForExperimentEnabled": False},
     "every 120 min / 200 mm (not during experiments)"),
])
def test_format_refill(opts, text):
    assert zp._format_refill(opts) == text


def test_zen_auto_immersion_settings_contract():
    # Live and read-only: whatever this machine has, it must not raise.
    info = zp.zen_auto_immersion_settings()
    assert set(info) == {"user_options_path", "user_options",
                         "service_options", "service_options_time"}
    assert isinstance(zp.format_auto_immersion_settings(info), str)
