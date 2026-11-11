# -*- coding: utf-8 -*-
"""Unit tests for MS_zenapi_objectivechanger (gRPC layer MOCKED).

Two fake service stubs record every RPC, so the tests check both the values
returned and which RPCs each function sends.
"""
import asyncio
import contextlib
from types import SimpleNamespace as NS

import pytest

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
pytest.importorskip("zen_api", reason="zen_api not resolvable on this machine")

import MS_zenapi_objectivechanger as oc  # noqa: E402


@contextlib.asynccontextmanager
async def _fake_channel(cfg):
    yield (None, None)


@pytest.fixture
def changer(monkeypatch):
    """Fake changer + optovar. Returns the state dict (positions, RPC log)."""
    state = {"obj": 2, "opt": 1, "rpc": []}
    objectives = NS(objectives=[NS(name="5x", position=2), NS(name="20x", position=3)])
    optovars = NS(optovars=[NS(name="2x", position=1), NS(name="1x", position=2)])

    class _Obj:
        def __init__(self, channel=None, metadata=None):
            pass

        async def get_objectives(self, req):
            state["rpc"].append("get_objectives")
            return objectives

        async def get_position(self, req):
            state["rpc"].append("obj.get_position")
            return NS(value=state["obj"])

        async def move_to(self, req):
            state["rpc"].append("obj.move_to")
            state["obj"] = req.position_index

    class _Opt:
        def __init__(self, channel=None, metadata=None):
            pass

        async def get_optovars(self, req):
            state["rpc"].append("get_optovars")
            return optovars

        async def get_position(self, req):
            state["rpc"].append("opt.get_position")
            return NS(value=state["opt"])

        async def move_to(self, req):
            state["rpc"].append("opt.move_to")
            state["opt"] = req.position_index

    monkeypatch.setattr(oc, "open_zen_channel", _fake_channel)
    monkeypatch.setattr(oc, "ObjectiveChangerServiceStub", _Obj)
    monkeypatch.setattr(oc, "OptovarServiceStub", _Opt)
    return state


def test_current_positions_reads_only_the_two_positions(changer):
    assert asyncio.run(oc.get_current_objective_and_optovar()) == (2, 1)
    assert changer["rpc"] == ["obj.get_position", "opt.get_position"]


def test_current_names(changer):
    assert asyncio.run(oc.get_current_objective_and_optovar_names()) == (("5x", 2), ("2x", 1))


def test_current_names_unknown_position_raises(changer):
    changer["obj"] = 4
    with pytest.raises(ValueError, match="objective position 4"):
        asyncio.run(oc.get_current_objective_and_optovar_names())


def test_set_moves_both(changer):
    asyncio.run(oc.set_objective_set_optovar(3, 2))
    assert (changer["obj"], changer["opt"]) == (3, 2)
    assert changer["rpc"].count("obj.move_to") == 1 and changer["rpc"].count("opt.move_to") == 1
