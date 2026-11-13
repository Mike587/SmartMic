# -*- coding: utf-8 -*-
"""Unit tests for MS_zen_logs (facts read from ZEN's log and options files).

The parsing logic is a pure helper, tested against synthetic text. The live
readers are checked only for their return-value contract (skipped where the
machine does not have the file). The DF carrier bottom is in test_df_carrier.py.
"""
import os

import pytest

import zeiss_paths as zp  # noqa: E402  (extends sys.path as a side effect)
import MS_zen_logs as zl  # noqa: E402


def test_last_software_version_picks_the_last_match():
    # ZEN.log.xml is a flat stream of <event> fragments (no wrapping root), each
    # stamping its own SoftwareVersion -- take the LAST one, not the first, so a
    # version that changed mid-file (a ZEN restart/upgrade) reflects what's
    # running now.
    text = (
        '<event><properties><data name="SoftwareVersion" value="3.12.9.0" /></properties></event>\n'
        '<event><properties><data name="SoftwareVersion" value="3.13.109.08000" /></properties></event>\n'
    )
    assert zl._last_software_version(text) == "3.13.109.08000"


def test_last_software_version_none_when_absent():
    assert zl._last_software_version("<event><message>no version here</message></event>") is None


def test_zen_app_version_missing_file_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(zp, "ZEN_LOGGING_DIR", tmp_path / "does_not_exist")
    assert zl.zen_app_version() is None


def test_zen_app_version_contract():
    log_file = zp.ZEN_LOGGING_DIR / "ZEN.log.xml"
    if not log_file.is_file():
        pytest.skip("ZEN.log.xml not present on this machine")
    version = zl.zen_app_version()
    assert version is None or isinstance(version, str)


def test_log_readers_follow_a_changed_logging_dir(tmp_path, monkeypatch):
    # MS_zen_logs reads zeiss_paths.ZEN_LOGGING_DIR at call time.
    monkeypatch.setattr(zp, "ZEN_LOGGING_DIR", tmp_path)
    assert zl.zen_app_version() is None
    (tmp_path / "ZEN.log.xml").write_text(
        '<data name="SoftwareVersion" value="9.9.9" />', encoding="utf-8")
    assert zl.zen_app_version() == "9.9.9"


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
    assert zl._coerce_option_value("True") is True
    assert zl._coerce_option_value("false") is False
    assert zl._coerce_option_value("120") == 120
    assert zl._coerce_option_value("0.5") == 0.5
    assert zl._coerce_option_value("abc") == "abc"


def test_immersion_options_from_options_config_keeps_only_the_sent_keys():
    opts = zl._immersion_options_from_options_config(_OPTIONS_CONFIG)
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
    assert zl._user_options_path_from_zen_log(text) == \
        r"C:\ProgramData\Carl Zeiss\ZEN\Users\mike\DefaultWorkspace\Options.config"


def test_user_options_path_none_when_absent():
    assert zl._user_options_path_from_zen_log(
        _zen_log_save(r"C:\ProgramData\Carl Zeiss\ZEN\Options.config")) is None


def test_last_service_immersion_options_picks_last_payload_with_keys():
    text = (_service_options_event("2026-11-06T08:37:22+01:00", 120)
            + _service_options_event("2026-11-09T10:04:06+01:00", 10)
            # A later SetServiceOptions without immersion keys must not hide it.
            + _service_options_event("2026-11-09T10:05:44+01:00", None))
    opts, ts = zl._last_service_immersion_options(text)
    assert ts == "2026-11-09T10:04:06+01:00"
    assert opts == {"IsAutoImmersionEnabled": True,
                    "AutoImmersionRefillInterval": 10,
                    "AutoImmersionRefillTravelDistance": 200}


def test_last_service_immersion_options_none_when_absent():
    assert zl._last_service_immersion_options(
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
    info = zl.zen_auto_immersion_settings()

    assert info["user_options_path"] == user_file
    assert info["user_options"]["AutoImmersionRefillInterval"] == 10
    assert info["service_options"]["AutoImmersionRefillInterval"] == 120
    assert info["service_options_time"] == "2026-11-09T10:04:06+01:00"

    line = zl.format_auto_immersion_settings(info)
    assert "ZEN user 'bob'" in line
    assert "every 10 min / distance off" in line
    assert "every 120 min / 200 mm" in line
    assert "MISMATCH" in line


def test_zen_auto_immersion_settings_missing_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(zp, "ZEN_LOGGING_DIR", tmp_path / "does_not_exist")
    info = zl.zen_auto_immersion_settings()
    assert info == {"user_options_path": None, "user_options": None,
                    "service_options": None, "service_options_time": None}
    line = zl.format_auto_immersion_settings(info)
    assert "ZEN user '?'" in line and "unknown" in line and "MISMATCH" not in line


def test_zen_auto_immersion_settings_user_file_gone(tmp_path, monkeypatch):
    # The log names a file that no longer exists: path reported, options None.
    logs = tmp_path / "Logging"
    logs.mkdir()
    gone = tmp_path / "ZEN" / "Users" / "bob" / "DefaultWorkspace" / "Options.config"
    _touch(logs / "ZEN.log.xml", _zen_log_save(gone), 2000)
    monkeypatch.setattr(zp, "ZEN_LOGGING_DIR", logs)
    info = zl.zen_auto_immersion_settings()
    assert info["user_options_path"] == gone
    assert info["user_options"] is None


@pytest.mark.parametrize("path, user", [
    (r"C:\ProgramData\Carl Zeiss\ZEN\Users\mike\DefaultWorkspace\Options.config", "mike"),
    (r"C:\ProgramData\Carl Zeiss\ZEN\Users\mike\Workspaces\MS01\Options.config", "mike"),
    (r"C:\ProgramData\Carl Zeiss\ZEN\DefaultUser\DefaultWorkspace\Options.config", "DefaultUser"),
])
def test_zen_user_from_options_path(path, user):
    assert zl._zen_user_from_options_path(path) == user


@pytest.mark.parametrize("opts, text", [
    (None, "unknown"),
    ({"IsAutoImmersionEnabled": False}, "auto-immersion OFF"),
    ({"IsAutoImmersionRefillGlobalEnabled": False}, "refill OFF"),
    ({"AutoImmersionRefillInterval": 120, "AutoImmersionRefillTravelDistance": 200,
      "IsAutoImmersionRefillForExperimentEnabled": False},
     "every 120 min / 200 mm (not during experiments)"),
])
def test_format_refill(opts, text):
    assert zl._format_refill(opts) == text


def test_zen_auto_immersion_settings_contract():
    # Live and read-only: whatever this machine has, it must not raise.
    info = zl.zen_auto_immersion_settings()
    assert set(info) == {"user_options_path", "user_options",
                         "service_options", "service_options_time"}
    assert isinstance(zl.format_auto_immersion_settings(info), str)
