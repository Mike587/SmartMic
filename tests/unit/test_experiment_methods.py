# -*- coding: utf-8 -*-
"""Unit tests for MS_zenapi_experiment_methods (gRPC layer MOCKED).

A fake ExperimentService "acquires" by writing an empty .czi into a fake ZEN
default output folder. The tests check run_experiment_from_path: load by path,
output naming, the move out of ZEN's default folder, the cleanup, and stop.
"""
import asyncio
import contextlib

import pytest

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
pytest.importorskip("zen_api", reason="zen_api not resolvable on this machine")

import MS_zenapi_experiment_methods as em  # noqa: E402


@contextlib.asynccontextmanager
async def _fake_channel(cfg):
    yield (None, None)


class _Resp:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def zen(tmp_path, monkeypatch):
    """A fake gateway. Returns (record, default_folder, custom_folder)."""
    default = tmp_path / "zen_default"
    default.mkdir()
    custom = tmp_path / "custom"
    rec = {"load": [], "run": []}

    class _Stub:
        def __init__(self, channel=None, metadata=None):
            pass

        async def get_image_output_path(self, req):
            return _Resp(image_output_path=str(default))

        async def load(self, req):
            rec["load"].append(req.experiment_name)
            return _Resp(experiment_id="loaded-id")

        async def run_experiment(self, req):
            rec["run"].append((req.experiment_id, req.output_name))
            (default / f"{req.output_name}.czi").write_bytes(b"czi")
            return _Resp(output_name=req.output_name)

        async def get_status(self, req):
            return _Resp(status="done")

    monkeypatch.setattr(em, "open_zen_channel", _fake_channel)
    monkeypatch.setattr(em, "ExperimentServiceStub", _Stub)
    czexp = tmp_path / "EXP.czexp"
    czexp.write_text("<HardwareExperiment/>", encoding="utf-8")
    return rec, default, custom, czexp


def _run(czexp, **kw):
    return asyncio.run(em.run_experiment_from_path(czexp, **kw))


def test_run_loads_runs_and_moves(zen):
    rec, default, custom, czexp = zen
    out = _run(czexp, custom_image_folder=custom, custom_filename="shot")

    assert rec["run"] == [("loaded-id", "shot")]
    assert out == {"exp_result_path": custom / "shot.czi", "experiment_id": "loaded-id"}
    assert (custom / "shot.czi").exists()
    assert not (default / "shot.czi").exists()


def test_run_from_path_loads_the_path_without_suffix(zen, tmp_path):
    rec, _, custom, _ = zen
    czexp = tmp_path / "my exp.czexp"
    czexp.write_text("<HardwareExperiment/>", encoding="utf-8")
    out = asyncio.run(em.run_experiment_from_path(
        czexp, custom_image_folder=custom, custom_filename="shot.czi"))

    assert rec["load"] == [str(tmp_path / "my exp")]
    assert rec["run"] == [("loaded-id", "shot")]        # ".czi" removed from the name
    assert out["exp_result_path"] == custom / "shot.czi"
    assert out["experiment_id"] == "loaded-id"


def test_run_from_path_missing_file_raises_before_any_rpc(zen, tmp_path):
    rec, _, custom, _ = zen
    with pytest.raises(FileNotFoundError):
        asyncio.run(em.run_experiment_from_path(tmp_path / "nope.czexp",
                                                custom_image_folder=custom))
    assert rec == {"load": [], "run": []}


def test_default_name_is_generated(zen):
    rec, _, custom, czexp = zen
    out = _run(czexp, custom_image_folder=custom)
    name = rec["run"][0][1]
    assert name.startswith("zenapi_") and len(name) == len("zenapi_") + 8
    assert out["exp_result_path"] == custom / f"{name}.czi"


def test_existing_result_gets_a_counter(zen):
    rec, _, custom, czexp = zen
    custom.mkdir()
    (custom / "shot.czi").write_bytes(b"earlier")
    (custom / "shot_000001.czi").write_bytes(b"earlier")
    out = _run(czexp, custom_image_folder=custom, custom_filename="shot")

    assert rec["run"] == [("loaded-id", "shot_000002")]
    assert out["exp_result_path"] == custom / "shot_000002.czi"
    assert (custom / "shot.czi").read_bytes() == b"earlier"


def test_other_czi_in_the_zen_default_folder_are_removed(zen):
    # Documented behaviour (DEV_NOTES "The cleanup deletes everything in ZEN's
    # default folder"): the folder is owned by the pipeline during a run.
    _, default, custom, czexp = zen
    (default / "stale.czi").write_bytes(b"x")
    (default / "keep.txt").write_text("x", encoding="utf-8")
    _run(czexp, custom_image_folder=custom, custom_filename="shot")

    assert not (default / "stale.czi").exists()
    assert (default / "keep.txt").exists()


def test_output_folder_equal_to_the_zen_default_folder_is_refused(zen):
    # The cleanup deletes every other .czi in ZEN's default folder: with the
    # results there, each acquisition would delete all earlier ones.
    rec, default, _, czexp = zen
    (default / "earlier_result.czi").write_bytes(b"keep me")
    with pytest.raises(ValueError, match="ZEN's default image folder"):
        _run(czexp, custom_image_folder=default, custom_filename="shot")
    assert rec["load"] == [] and rec["run"] == []          # nothing loaded or run
    assert (default / "earlier_result.czi").exists()       # nothing deleted


def test_same_folder_in_another_spelling_is_refused(zen):
    rec, default, _, czexp = zen
    other_spelling = default.parent / "." / default.name.upper()
    with pytest.raises(ValueError, match="ZEN's default image folder"):
        _run(czexp, custom_image_folder=other_spelling)
    assert rec["run"] == []


# --------------------------------------------------------------------------
# stop_running_experiment
# --------------------------------------------------------------------------
@pytest.fixture
def stoppable(monkeypatch):
    """Fake gateway with a status and a Stop RPC. Returns the state dict."""
    from grpclib import GRPCError
    from grpclib.const import Status

    state = {"running": True, "stopped": []}

    class _Stub:
        def __init__(self, channel=None, metadata=None):
            pass

        async def get_status(self, req):
            if not state["running"]:
                raise GRPCError(Status.FAILED_PRECONDITION, "No experiment is running")
            return _Resp(status=_Resp(
                is_experiment_running=True, is_acquisition_running=True,
                images_acquired_index=1, images_count=9, scenes_index=0, scenes_count=1))

        async def stop(self, req):
            state["stopped"].append(req.experiment_id)

    monkeypatch.setattr(em, "open_zen_channel", _fake_channel)
    monkeypatch.setattr(em, "ExperimentServiceStub", _Stub)
    monkeypatch.setattr(em, "_last_started_experiment_id", None)
    return state


def test_stop_sends_stop_for_the_experiment_this_process_started(stoppable, monkeypatch):
    monkeypatch.setattr(em, "_last_started_experiment_id", "loaded-id")
    assert asyncio.run(em.stop_running_experiment()) is True
    assert stoppable["stopped"] == ["loaded-id"]


def test_stop_without_a_known_id_sends_no_id(stoppable):
    assert asyncio.run(em.stop_running_experiment()) is True
    assert stoppable["stopped"] == [""]


def test_stop_sends_nothing_when_idle(stoppable):
    stoppable["running"] = False
    assert asyncio.run(em.stop_running_experiment()) is False
    assert stoppable["stopped"] == []


def test_a_run_records_its_experiment_id_for_stop(zen, monkeypatch):
    _, _, custom, czexp = zen
    monkeypatch.setattr(em, "_last_started_experiment_id", None)
    _run(czexp, custom_image_folder=custom, custom_filename="shot")
    assert em._last_started_experiment_id == "loaded-id"
