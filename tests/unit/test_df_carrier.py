# -*- coding: utf-8 -*-
"""Unit tests: the carrier bottom (material, thickness) that DefiniteFocus uses.

Measured on the CD7 on 2026-11-09 (DEV_NOTES "Dry objectives: DF finds the plate
UNDERSIDE"): a wrong thickness or material moves the DF focus by tens of µm, and
ZEN sends thickness 0 to DF during a tray load. Covered here:
- zeiss_paths._last_df_bottom / zen_df_bottom: the value DF received (ZEN log),
- check_carrier_for_df (preflight),
- the per-call carrier compare in DefiniteFocusTracker,
- wait_for_df_bottom (after a tray load).
All hardware calls are MOCKED.
"""
import os

import pytest

import zeiss_paths as zp  # noqa: F401  — extends sys.path so zen_api resolves
pytest.importorskip("zen_api", reason="zen_api not resolvable on this machine")

import MS_CD7_API_LoA as ms  # noqa: E402


def _event(ts, material, nm):
    return (f'<event logger="Zeiss.Micro.LM.Components.Mtb.MtbFocusStabilizerLiveCellScanner" '
            f'timestamp="{ts}" level="DEBUG" thread="x" domain="ZENService"><message>'
            f'[MTBFocusStabilizerLiveCellScanner] MtbBottomPropertiesChangedEvent(): '
            f'material = {material}, thickness = {nm}</message><properties /></event>\n')


def _carrier(material="Glass", thickness=170.0, name="PLATE", n=1.52):
    return {"name": name, "rows": 2, "columns": 3, "material": material,
            "thickness": thickness, "skirt": 1044.0, "refractive_index": n}


class _Log:
    def __init__(self):
        self.infos, self.warnings, self.errors = [], [], []

    def info(self, m):
        self.infos.append(m)

    def warning(self, m):
        self.warnings.append(m)

    def error(self, m):
        self.errors.append(m)


# --------------------------------------------------------------------------
# The value DF received, from the ZEN log
# --------------------------------------------------------------------------
def test_last_df_bottom_takes_the_last_event():
    text = (_event("2026-11-09T15:23:40+01:00", "Glass", 0)
            + _event("2026-11-09T15:24:16+01:00", "Glass", 168000)
            + _event("2026-11-09T15:36:44+01:00", "Polystyrene", 176179))
    assert zp._last_df_bottom(text) == {"timestamp": "2026-11-09T15:36:44+01:00",
                                        "material": "Polystyrene", "thickness_um": 176.179}
    assert zp._last_df_bottom("<event><message>nothing</message></event>") is None


def test_zen_df_bottom_searches_rotated_logs(tmp_path, monkeypatch):
    logs = tmp_path / "Logging"
    logs.mkdir()

    def touch(name, text, mtime):
        (logs / name).write_text(text, encoding="utf-8")
        os.utime(logs / name, (mtime, mtime))

    touch("ZENService.log.xml.2", _event("2026-11-09T10:00:00+01:00", "Glass", 170000), 1000)
    touch("ZENService.log.xml", "<event><message>nothing</message></event>\n", 3000)
    touch("ZENService.log.xml.1", _event("2026-11-09T11:00:00+01:00", "Glass", 168000), 2000)
    monkeypatch.setattr(zp, "ZEN_LOGGING_DIR", logs)
    assert zp.zen_df_bottom()["thickness_um"] == 168.0      # newest file with the event

    monkeypatch.setattr(zp, "ZEN_LOGGING_DIR", tmp_path / "does_not_exist")
    assert zp.zen_df_bottom() is None


# --------------------------------------------------------------------------
# check_carrier_for_df (preflight)
# --------------------------------------------------------------------------
@pytest.fixture
def carrier(monkeypatch):
    def install(api=None, df=None, measure=True):
        monkeypatch.setattr(ms, "get_sample_carrier_info", lambda: api or _carrier())
        monkeypatch.setattr(ms.MS_zen_logs, "zen_df_bottom", lambda: df)
        monkeypatch.setattr(ms, "get_live_scan_configuration",
                            lambda: {"measure_bottom_thickness": measure})
    return install


def test_check_passes_and_returns_snapshot(carrier):
    carrier(df={"timestamp": "t", "material": "Glass", "thickness_um": 170.0})
    log = _Log()
    snap = ms.check_carrier_for_df(log, expected_material="glass", thickness_range_um=(150, 190))
    assert snap["thickness_um"] == 170.0 and snap["material"] == "Glass"
    assert log.errors == [] and log.warnings == []


@pytest.mark.parametrize("api, df, kwargs, text", [
    (_carrier(thickness=0.0), None, {}, "thickness 0"),
    (_carrier(material="Polystyrene"), None, {"expected_material": "Glass"}, "not Glass"),
    (_carrier(thickness=250.0), None, {"thickness_range_um": (150, 190)}, "not in"),
    (_carrier(), {"timestamp": "t", "material": "Glass", "thickness_um": 0.0}, {}, "DF has thickness 0"),
    (_carrier(), {"timestamp": "t", "material": "Glass", "thickness_um": 250.0}, {}, "different value"),
    (_carrier(), {"timestamp": "t", "material": "Polystyrene", "thickness_um": 170.0}, {},
     "different value"),
])
def test_check_fails(carrier, api, df, kwargs, text):
    carrier(api=api, df=df)
    log = _Log()
    assert ms.check_carrier_for_df(log, **kwargs) is None
    assert any(text in e for e in log.errors)


def test_check_without_log_event_uses_api_only(carrier):
    carrier(df=None)
    log = _Log()
    assert ms.check_carrier_for_df(log) is not None
    assert any("not in the ZEN logs" in m for m in log.infos)


def test_check_rounding_between_api_and_log_is_ok(carrier):
    carrier(api=_carrier(material="Polystyrene", thickness=176.17894736842106, n=1.594),
            df={"timestamp": "t", "material": "Polystyrene", "thickness_um": 176.179})
    assert ms.check_carrier_for_df(_Log()) is not None


def test_check_warns_if_thickness_is_not_measured_at_load(carrier):
    carrier(measure=False)
    log = _Log()
    assert ms.check_carrier_for_df(log) is not None
    assert any("measure bottom thickness" in w for w in log.warnings)


def test_preflight_aborts_on_carrier_check(carrier, monkeypatch):
    carrier(api=_carrier(material="Polystyrene"))
    monkeypatch.setattr(ms, "get_sample_carrier_name", lambda: "PLATE")
    monkeypatch.setattr(ms, "is_microscope_busy", lambda: False)
    motion = []
    monkeypatch.setattr(ms, "set_stage_motion_sync",
                        lambda s=None, a=None: motion.append(1) or {})
    log = _Log()
    assert ms.preflight("PLATE", log, expected_material="Glass") is False
    assert motion == []          # nothing is set when a check fails


# --------------------------------------------------------------------------
# DefiniteFocusTracker: per-call compare
# --------------------------------------------------------------------------
@pytest.fixture
def tracker_env(monkeypatch):
    state = {"carrier": _carrier(), "searches": 0}

    def find(max_retries=3, start_z_m=None):
        state["searches"] += 1
        return True, "ok", 1

    monkeypatch.setattr(ms, "get_sample_carrier_info", lambda: dict(state["carrier"]))
    monkeypatch.setattr(ms, "run_definite_focus_find_surface", find)
    monkeypatch.setattr(ms, "get_current_z_position", lambda: 1.2e-3)
    monkeypatch.setattr(ms, "get_current_objective_and_optovar", lambda: (1, 1))
    return state


def test_tracker_records_then_accepts_same_carrier(tracker_env):
    t = ms.DefiniteFocusTracker()
    t.find_surface()
    t.find_surface()
    assert t.carrier["thickness_um"] == 170.0 and tracker_env["searches"] == 2


@pytest.mark.parametrize("change", [{"thickness": 250.0}, {"material": "Polystyrene"},
                                    {"name": "OTHER"}])
def test_tracker_stops_on_changed_carrier(tracker_env, change):
    t = ms.DefiniteFocusTracker()
    t.find_surface()
    tracker_env["carrier"].update(change)
    with pytest.raises(ms.CarrierChanged):
        t.find_surface()
    assert tracker_env["searches"] == 1          # no search with the changed carrier


def test_tracker_stops_on_zero_thickness(tracker_env):
    tracker_env["carrier"]["thickness"] = 0.0
    with pytest.raises(ms.CarrierChanged):
        ms.DefiniteFocusTracker().find_surface()
    assert tracker_env["searches"] == 0


def test_carrier_changed_is_a_df_fault():
    # Pipelines that stop on DefiniteFocusFault (smartmic_poc) also stop here.
    assert issubclass(ms.CarrierChanged, ms.DefiniteFocusFault)


def test_tracker_reset_accepts_new_carrier(tracker_env):
    t = ms.DefiniteFocusTracker()
    t.find_surface()
    tracker_env["carrier"]["thickness"] = 168.0
    t.reset()                                     # e.g. after a tray load
    t.find_surface()
    assert t.carrier["thickness_um"] == 168.0


def test_tracker_carrier_check_can_be_off(tracker_env):
    t = ms.DefiniteFocusTracker(carrier_check=False)
    tracker_env["carrier"]["thickness"] = 0.0
    t.find_surface()
    assert t.carrier is None and tracker_env["searches"] == 1


# --------------------------------------------------------------------------
# wait_for_df_bottom (after a tray load)
# --------------------------------------------------------------------------
def _sequence(monkeypatch, api_values, df_values):
    api = iter(api_values)
    df = iter(df_values)
    monkeypatch.setattr(ms, "get_sample_carrier_info", lambda: next(api))
    monkeypatch.setattr(ms.MS_zen_logs, "zen_df_bottom", lambda: next(df))
    monkeypatch.setattr(ms.time, "sleep", lambda s: None)


def test_wait_returns_after_two_stable_non_zero_reads(monkeypatch):
    zero = _carrier(thickness=0.0)
    ok = _carrier(thickness=168.0)
    df_ok = {"timestamp": "t", "material": "Glass", "thickness_um": 168.0}
    _sequence(monkeypatch, [zero, ok, ok], [df_ok, df_ok])   # df not read while API is 0
    assert ms.wait_for_df_bottom(timeout_s=10, poll_s=0)["thickness_um"] == 168.0


def test_wait_keeps_waiting_while_df_has_zero(monkeypatch):
    ok = _carrier(thickness=168.0)
    df_zero = {"timestamp": "t", "material": "Glass", "thickness_um": 0.0}
    df_ok = {"timestamp": "t", "material": "Glass", "thickness_um": 168.0}
    _sequence(monkeypatch, [ok, ok, ok, ok], [df_zero, df_zero, df_ok, df_ok])
    assert ms.wait_for_df_bottom(timeout_s=10, poll_s=0)["thickness_um"] == 168.0


def test_wait_times_out(monkeypatch):
    zero = _carrier(thickness=0.0)
    monkeypatch.setattr(ms, "get_sample_carrier_info", lambda: zero)
    monkeypatch.setattr(ms.MS_zen_logs, "zen_df_bottom", lambda: None)
    monkeypatch.setattr(ms.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError):
        ms.wait_for_df_bottom(timeout_s=0, poll_s=0)
