# -*- coding: utf-8 -*-
"""Unit tests for MS_zenapi_helpers (no zen_api needed — stdlib + grpclib only)."""
import asyncio
from types import SimpleNamespace

import pytest

import MS_zenapi_helpers as zh


def _item(pos):
    return SimpleNamespace(position=pos, name=f"item{pos}")


# --------------------------------------------------------------------------
# Objective / optovar position lookups (duck-typed)
# --------------------------------------------------------------------------
def test_get_objective_by_position():
    objs = SimpleNamespace(objectives=[_item(1), _item(3)])
    assert zh.get_objective_by_position(objs, 3).position == 3
    assert zh.get_objective_by_position(objs, 2) is None


def test_get_optovar_by_position():
    opts = SimpleNamespace(optovars=[_item(1), _item(2)])
    assert zh.get_optovar_by_position(opts, 2).position == 2
    assert zh.get_optovar_by_position(opts, 9) is None


def test_get_used_positions():
    objs = SimpleNamespace(objectives=[_item(1), _item(2), _item(4)])
    opts = SimpleNamespace(optovars=[_item(1), _item(3)])
    assert zh.get_used_objective_positions(objs) == [1, 2, 4]
    assert zh.get_used_optovar_positions(opts) == [1, 3]


# --------------------------------------------------------------------------
# set_logging
# --------------------------------------------------------------------------
def test_set_logging_idempotent():
    name = "smartmic_helpers_test"
    log1 = zh.set_logging(name)
    n = len(log1.handlers)
    log2 = zh.set_logging(name)
    assert log2 is log1
    assert n >= 1
    assert len(log2.handlers) == n   # no duplicate handlers on repeated calls


# --------------------------------------------------------------------------
# open_zen_channel — must close the channel on every exit path
# --------------------------------------------------------------------------
def _fake_channel():
    ch = SimpleNamespace(closed=False)
    ch.close = lambda: setattr(ch, "closed", True)
    return ch


def test_open_zen_channel_closes_on_success(monkeypatch):
    ch = _fake_channel()
    monkeypatch.setattr(zh, "initialize_zenapi", lambda cf: (ch, [("control-token", "x")]))

    async def body():
        async with zh.open_zen_channel("cfg") as (channel, md):
            assert channel is ch
            assert md == [("control-token", "x")]

    asyncio.run(body())
    assert ch.closed is True


def test_open_zen_channel_closes_on_exception(monkeypatch):
    ch = _fake_channel()
    monkeypatch.setattr(zh, "initialize_zenapi", lambda cf: (ch, []))

    async def body():
        async with zh.open_zen_channel("cfg") as (channel, md):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(body())
    assert ch.closed is True


# --------------------------------------------------------------------------
# Default time limits of the gRPC calls
# --------------------------------------------------------------------------
@pytest.mark.parametrize("route, expected", [
    ("/zen_api.lm.hardware.v2.StageService/GetPosition", zh.RPC_TIMEOUT_S),
    ("/zen_api.lm.hardware.v2.StageService/SetAcceleration", zh.RPC_TIMEOUT_S),
    ("/zen_api.lm.hardware.v2.ObjectiveChangerService/GetObjectives", zh.RPC_TIMEOUT_S),
    ("/zen_api.lm.hardware.v1.SampleCarrierService/GetInfo", zh.RPC_TIMEOUT_S),
    ("/zen_api.acquisition.v1beta.ExperimentService/Load", zh.RPC_TIMEOUT_S),
    ("/zen_api.acquisition.v1beta.ExperimentService/GetStatus", zh.RPC_TIMEOUT_S),
    ("/zen_api.acquisition.v1beta.ExperimentService/Stop", zh.RPC_TIMEOUT_S),
    ("/zen_api.lm.hardware.v2.StageService/MoveTo", zh.MOVE_RPC_TIMEOUT_S),
    ("/zen_api.lm.hardware.v2.FocusService/MoveTo", zh.MOVE_RPC_TIMEOUT_S),
    ("/zen_api.lm.hardware.v2.ObjectiveChangerService/MoveTo", zh.MOVE_RPC_TIMEOUT_S),
    ("/zen_api.lm.hardware.v1.SampleCarrierService/MoveToContainer", zh.MOVE_RPC_TIMEOUT_S),
    ("/zen_api.lm.acquisition.v1.DefiniteFocusService/FindSurface", zh.MOVE_RPC_TIMEOUT_S),
    ("/zen_api.lm.acquisition.v1.DefiniteFocusService/RecallFocus", zh.MOVE_RPC_TIMEOUT_S),
    # No limit: an acquisition, a stream, the tray RPCs, SWAF.
    ("/zen_api.acquisition.v1beta.ExperimentService/RunExperiment", None),
    ("/zen_api.acquisition.v1beta.ExperimentService/RunSnap", None),
    ("/zen_api.acquisition.v1beta.ExperimentService/StartContinuous", None),
    ("/zen_api.acquisition.v1beta.ExperimentService/RegisterOnStatusChanged", None),
    ("/zen_api.lm.live_scan.v1.LiveScanService/LoadTrayAndPrescan", None),
    ("/zen_api.lm.acquisition.v1.ExperimentSwAutofocusService/FindAutoFocus", None),
])
def test_default_rpc_timeout(route, expected):
    assert zh.default_rpc_timeout(route) == expected


def test_move_limit_is_longer_than_the_read_limit():
    assert zh.MOVE_RPC_TIMEOUT_S > zh.RPC_TIMEOUT_S > 0


@pytest.fixture
def sent(monkeypatch):
    """Record the timeout that ZenChannel hands to grpclib for each request."""
    seen = []

    def fake_request(self, name, cardinality, request_type, reply_type, *,
                     timeout=None, deadline=None, metadata=None):
        seen.append((name, timeout, deadline))
        return "stream"

    monkeypatch.setattr(zh.Channel, "request", fake_request)
    return seen


def _zen_channel():
    return zh.ZenChannel.__new__(zh.ZenChannel)     # no socket, no event loop


def test_zen_channel_adds_the_default_limit(sent):
    ch = _zen_channel()
    assert ch.request("/x.StageService/GetPosition", None, None, None) == "stream"
    ch.request("/x.StageService/MoveTo", None, None, None)
    ch.request("/x.ExperimentService/RunExperiment", None, None, None)
    assert [t for _, t, _ in sent] == [zh.RPC_TIMEOUT_S, zh.MOVE_RPC_TIMEOUT_S, None]
    assert ch.last_route == "/x.ExperimentService/RunExperiment"


def test_zen_channel_keeps_the_callers_limit(sent):
    ch = _zen_channel()
    ch.request("/x.StageService/GetPosition", None, None, None, timeout=5.0)
    ch.request("/x.StageService/GetPosition", None, None, None, deadline="D")
    assert sent[0][1] == 5.0
    assert sent[1][1] is None and sent[1][2] == "D"     # a deadline is not overridden


def test_timeout_inside_the_channel_names_the_call(monkeypatch):
    ch = _fake_channel()
    ch.last_route, ch.last_timeout = "/x.StageService/GetPosition", 60.0
    monkeypatch.setattr(zh, "initialize_zenapi", lambda cf: (ch, []))

    async def body():
        async with zh.open_zen_channel("cfg"):
            raise asyncio.TimeoutError()

    with pytest.raises(asyncio.TimeoutError, match=r"StageService/GetPosition .* 60 s"):
        asyncio.run(body())
    assert ch.closed is True
