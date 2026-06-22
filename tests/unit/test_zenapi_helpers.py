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
