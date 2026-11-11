"""Dry run of the PoC loop: a fake microscope and a fake analysis, the real
position file and base experiments. Nothing talks to ZEN.

    pixi run -e smartmic test-poc
"""
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import MS_SmartMic_PoC as poc  # noqa: E402
from MS_run_log import read_jsonl  # noqa: E402

SURFACE = 3.1e-3


class FakeScope:
    """Records every hardware call. `df` = outcomes of DF searches (True/False),
    consumed in order (then always True); `overview_error` makes every overview raise."""
    class FocusSearchFailed(RuntimeError):
        pass

    class DefiniteFocusFault(RuntimeError):
        pass

    class PreflightFailed(RuntimeError):
        pass

    def __init__(self, df=(), preflight_ok=True, swaf_ok=True, overview_error=None, df_fault=False):
        self.calls, self.df, self.preflight_ok, self.swaf_ok = [], list(df), preflight_ok, swaf_ok
        self.overview_error, self.df_fault = overview_error, df_fault
        scope = self

        class Tracker:
            last_message = ""

            def find_surface(self):
                scope.calls.append(("df",))
                if scope.df_fault:
                    raise scope.DefiniteFocusFault("5 positions in a row")
                ok = scope.df.pop(0) if scope.df else True
                self.last_message = "" if ok else "surface not found"
                return SURFACE if ok else None
        self.DefiniteFocusTracker = Tracker

    def preflight(self, carrier, log):
        self.calls.append(("preflight", carrier))
        if not self.preflight_ok:
            raise self.PreflightFailed("wrong carrier")

    def set_optics_verified(self, obj, opt):
        self.calls.append(("optics", obj, opt))

    def move_stage_to_new_xy_position(self, x, y):
        self.calls.append(("xy", round(x, 9), round(y, 9)))

    def swaf_find_focus(self, czexp):
        self.calls.append(("swaf", Path(czexp).name))
        if not self.swaf_ok:
            raise self.FocusSearchFailed("no focus")
        return SURFACE + 2e-6

    def run_experiment_from_path(self, czexp, folder, name):
        self.calls.append(("run", Path(czexp).name, name))
        if self.overview_error and name.endswith("_overview"):
            raise self.overview_error
        return {"exp_result_path": Path(folder) / f"{name}.czi", "experiment_id": "x"}

    def stop_experiment(self):
        self.calls.append(("stop",))
        return False

    def runs(self, suffix=""):
        return [c[2] for c in self.calls if c[0] == "run" and c[2].endswith(suffix)]


def targets(n, x0=0.05, y0=0.03):
    return [{"id": i + 1, "x_m": x0 + i * 10e-6, "y_m": y0, "area_m2": 6e-11, "row": 1, "col": 1}
            for i in range(n)]


@pytest.fixture
def run(tmp_path, monkeypatch):
    """Install a fake scope + analysis; returns (install, analyses) where
    analyses maps a position tag to its number of nuclei (missing → 4)."""
    analyses, calls = {}, []
    monkeypatch.setattr(poc, "RUN_FOLDER", tmp_path / "run")
    monkeypatch.setattr(poc, "LOG_FOLDER", tmp_path / "run" / "logs")
    monkeypatch.setattr(poc, "RECORDS", tmp_path / "run" / "run_positions.jsonl")
    monkeypatch.setattr(poc, "POSITIONS_TO_RUN", ["C4_P1", "C4_P2", "C5_P1"])
    monkeypatch.setattr(poc, "write_run_info", lambda *a, **k: None)
    monkeypatch.setattr(poc, "ANALYSIS_SCRIPT", Path(__file__))     # "present"

    def analysis(image, folder, tag, log, script, suffix, adir):
        calls.append((tag, suffix, Path(image).name))
        n = analyses.get(tag, 4)
        if n is None:
            return None                                             # analysis failed
        return {"found": n > 0, "n_nuclei": n, "targets": targets(n)}
    monkeypatch.setattr(poc, "run_analysis_json", analysis)

    def install(**kw):
        scope = FakeScope(**kw)
        monkeypatch.setattr(poc, "ms", scope)
        return scope
    install.analysis_calls = calls
    return install, analyses


def records():
    return read_jsonl(poc.RECORDS)


def test_full_run_images_n_nuclei_per_position(run):
    install, analyses = run
    analyses["C4_P2"] = 0                                           # an empty position
    scope = install()
    assert poc.main() == 0

    assert [c for c in scope.calls if c[0] in ("preflight", "optics")] == [
        ("preflight", "Multichamber 384"), ("optics", 3, 1)]        # optics once, after preflight
    assert [(r["position"], r["status"]) for r in records()] == [
        ("C4_P1", "ok"), ("C4_P2", "no_nuclei"), ("C5_P1", "ok")]
    ok = records()[0]
    assert len(ok["nuclei"]) == poc.N_NUCLEI and all(n["status"] == "ok" for n in ok["nuclei"])
    assert ok["z_df_m"] == SURFACE and ok["z_swaf_m"] == pytest.approx(SURFACE + 2e-6)
    assert len(scope.runs("_zstack")) == 2 * poc.N_NUCLEI
    assert len(scope.runs("_plane")) == 2 * poc.N_NUCLEI
    assert scope.runs("_overview") == ["C4_P1_overview", "C4_P2_overview", "C5_P1_overview"]
    # the analysis gets the overview, suffix "targets"
    assert run[0].analysis_calls[0] == ("C4_P1", "targets", "C4_P1_overview.czi")


def test_each_nucleus_uses_the_lsm_swaf_then_plane_then_zstack(run):
    install, analyses = run
    analyses.update({"C4_P1": 1, "C4_P2": 0, "C5_P1": 0})
    scope = install()
    poc.main()
    seq = scope.calls[scope.calls.index(("run", "DAPI_GFP_001.czexp", "C4_P1_overview")) + 1:]
    seq = seq[:seq.index(("run", "DAPI_GFP_001.czexp", "C4_P2_overview"))]
    names = [c[1] if c[0] in ("swaf", "run") else c[0] for c in seq if c[0] != "xy"][:5]
    assert names == ["df", "DAPI_LSM_onez_001_swaf_001.czexp", "DAPI_LSM_onez_001_swaf_002.czexp",
                     "DAPI_LSM_onez_001.czexp", "DAPI_LSM_z-stack_001.czexp"]
    t = targets(1)[0]
    assert ("xy", round(t["x_m"], 9), round(t["y_m"], 9)) in seq   # moved to the nucleus


def test_nucleus_choice_is_reproducible(run):
    install, analyses = run
    analyses.update({"C4_P1": 50, "C4_P2": 0, "C5_P1": 0})
    install()
    poc.main()
    first = [n["id"] for n in records()[0]["nuclei"]]
    poc.RECORDS.unlink()
    install()
    poc.main()
    assert [n["id"] for n in records()[0]["nuclei"]] == first


def test_resume_skips_finished_and_retries_errors(run):
    install, _ = run
    install(df=[True] * (1 + poc.N_NUCLEI) + [False])               # C4_P2's overview DF fails
    poc.main()
    assert [r["status"] for r in records()] == ["ok", "no_surface", "ok"]
    scope = install()
    poc.main()
    assert scope.runs("_overview") == ["C4_P2_overview"]            # only the unfinished one
    assert [r["status"] for r in records()][-1] == "ok"


def test_failed_swaf_keeps_going(run):
    install, analyses = run
    analyses.update({"C4_P1": 1, "C4_P2": 0, "C5_P1": 0})
    install(swaf_ok=False)
    assert poc.main() == 0
    assert records()[0]["status"] == "ok" and records()[0]["z_swaf_m"] is None


def test_analysis_failure_is_recorded_and_retried(run):
    install, analyses = run
    analyses["C4_P1"] = None
    install()
    poc.main()
    assert records()[0]["status"] == "analysis_failed"
    assert "nuclei" not in records()[0]


def test_consecutive_errors_stop_the_run(run, monkeypatch):
    install, _ = run
    monkeypatch.setattr(poc, "MAX_CONSECUTIVE_ERRORS", 2)
    scope = install(overview_error=RuntimeError("ZEN said no"))
    assert poc.main() == 3
    assert [r["status"] for r in records()] == ["error", "error"]
    assert "ZEN said no" in records()[0]["error"]


def test_df_fault_stops_the_run(run):
    install, _ = run
    scope = install(df_fault=True)
    assert poc.main() == 2
    assert records() == [] and scope.runs() == []


def test_preflight_failure_moves_nothing(run):
    install, _ = run
    scope = install(preflight_ok=False)
    assert poc.main() == 1
    assert [c[0] for c in scope.calls] == ["preflight"]


def test_unknown_position_is_refused_before_any_move(run, monkeypatch):
    install, _ = run
    monkeypatch.setattr(poc, "POSITIONS_TO_RUN", ["Z99_P1"])
    scope = install()
    assert poc.main() == 1
    assert [c[0] for c in scope.calls] == ["preflight"]


def test_ctrl_c_stops_the_experiment(run):
    install, _ = run
    scope = install(overview_error=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        poc.main()
    assert scope.calls[-1] == ("stop",)
