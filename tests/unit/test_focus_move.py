# -*- coding: utf-8 -*-
"""Unit tests for MS_zenapi_focus.move_focus_to_new_z_position's error handling.

Pins the behaviour of the near-zero fallback (already within 10 µm of 0 → ok;
else try 10 µm) and that a failed non-zero move re-raises the ORIGINAL error
without the old misleading "Could not get current position" log. The
FocusService stub is MOCKED.
"""
import asyncio
import contextlib
import logging

import pytest

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
pytest.importorskip("zen_api", reason="zen_api not resolvable on this machine")

import MS_zenapi_focus as focus   # noqa: E402


class MoveError(Exception):
    pass


class _Pos:
    def __init__(self, value):
        self.value = value


def _install(monkeypatch, move_outcomes, position=0.0, read_error=None):
    """Fake FocusService: move_to pops outcomes (exception → raised)."""
    moves = []

    class _Stub:
        def __init__(self, channel=None, metadata=None):
            pass

        async def move_to(self, req):
            moves.append(req.value)
            out = move_outcomes.pop(0)
            if isinstance(out, BaseException):
                raise out

        async def get_position(self, req):
            if read_error is not None:
                raise read_error
            return _Pos(position)

    @contextlib.asynccontextmanager
    async def _channel(cfg):
        yield (None, None)

    monkeypatch.setattr(focus, "FocusServiceStub", _Stub)
    monkeypatch.setattr(focus, "open_zen_channel", _channel)
    return moves


def test_nonzero_failure_reraises_original_without_misleading_log(monkeypatch, caplog):
    err = MoveError("Requested position was not reached.")
    _install(monkeypatch, [err], position=1.35e-3)
    with caplog.at_level(logging.INFO, logger="smartmic"):
        with pytest.raises(MoveError) as exc:
            asyncio.run(focus.move_focus_to_new_z_position(1.35e-3))
    assert exc.value is err
    assert "Could not get current position" not in caplog.text


def test_nonzero_failure_with_unreadable_position_reraises_original(monkeypatch, caplog):
    err = MoveError("not reached")
    _install(monkeypatch, [err], read_error=MoveError("read failed"))
    with caplog.at_level(logging.INFO, logger="smartmic"):
        with pytest.raises(MoveError) as exc:
            asyncio.run(focus.move_focus_to_new_z_position(1.35e-3))
    assert exc.value is err
    assert "Could not get current position: read failed" in caplog.text


def test_zero_target_already_close_is_ok(monkeypatch):
    moves = _install(monkeypatch, [MoveError("not reached")], position=4e-6)
    asyncio.run(focus.move_focus_to_new_z_position(0))
    assert moves == [0]


def test_zero_target_falls_back_to_10um(monkeypatch):
    moves = _install(monkeypatch, [MoveError("not reached"), None], position=50e-6)
    asyncio.run(focus.move_focus_to_new_z_position(0))
    assert moves == [0, 10e-6]


def test_zero_target_fallback_failure_reraises_original(monkeypatch):
    err = MoveError("not reached")
    _install(monkeypatch, [err, MoveError("alt failed")], position=50e-6)
    with pytest.raises(MoveError) as exc:
        asyncio.run(focus.move_focus_to_new_z_position(0))
    assert exc.value is err
