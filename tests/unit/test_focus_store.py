# -*- coding: utf-8 -*-
"""Unit tests: when MS_zenapi_focus.definite_focus_find_surface does StoreFocus.

Decided 2026-11-09 (DEV_NOTES "What each DF RPC does"):
- StoreFocus only if the caller asks for it (store_focus=True).
- Never StoreFocus after a failed search (an offset at a random Z sends a later
  RecallFocus to an incorrect Z).
The FocusService and DefiniteFocusService stubs are MOCKED.
"""
import asyncio
import contextlib

import pytest

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
pytest.importorskip("zen_api", reason="zen_api not resolvable on this machine")

import MS_zenapi_focus as focus   # noqa: E402
import MS_CD7_API_LoA as ms       # noqa: E402


class SearchError(Exception):
    pass


class _Found:
    def __init__(self, z):
        self.zposition = z


class _Pos:
    def __init__(self, value):
        self.value = value


def _install(monkeypatch, find_outcomes):
    """Fake stubs. find_outcomes: list of Z values (m) or exceptions, one per attempt."""
    calls = {"find": 0, "store": 0, "moves": []}

    class _Focus:
        def __init__(self, channel=None, metadata=None):
            pass

        async def move_to(self, req):
            calls["moves"].append(req.value)

        async def get_position(self, req):
            return _Pos(1.2e-3)

    class _DF:
        def __init__(self, channel=None, metadata=None):
            pass

        async def find_surface(self, req):
            calls["find"] += 1
            out = find_outcomes.pop(0)
            if isinstance(out, BaseException):
                raise out
            return _Found(out)

        async def store_focus(self, req):
            calls["store"] += 1

    @contextlib.asynccontextmanager
    async def _channel(cfg):
        yield (None, None)

    monkeypatch.setattr(focus, "FocusServiceStub", _Focus)
    monkeypatch.setattr(focus, "DefiniteFocusServiceStub", _DF)
    monkeypatch.setattr(focus, "open_zen_channel", _channel)
    monkeypatch.setattr(focus, "DF_RETRY_BACKOFF_S", 0.0)
    return calls


def test_success_does_not_store_by_default(monkeypatch):
    calls = _install(monkeypatch, [1.2e-3])
    assert asyncio.run(focus.definite_focus_find_surface()) == (True, 1)
    assert calls["store"] == 0


def test_success_stores_when_asked(monkeypatch):
    calls = _install(monkeypatch, [1.2e-3])
    assert asyncio.run(focus.definite_focus_find_surface(store_focus=True)) == (True, 1)
    assert calls["store"] == 1


def test_retry_then_success_stores_once_when_asked(monkeypatch):
    calls = _install(monkeypatch, [SearchError("ReflexNotFound"), 1.2e-3])
    assert asyncio.run(focus.definite_focus_find_surface(store_focus=True)) == (True, 2)
    assert calls["store"] == 1


@pytest.mark.parametrize("store_focus", [False, True])
def test_total_failure_never_stores(monkeypatch, store_focus):
    calls = _install(monkeypatch, [SearchError("a"), SearchError("b"), SearchError("c")])
    with pytest.raises(SearchError) as exc:
        asyncio.run(focus.definite_focus_find_surface(max_retries=3, store_focus=store_focus))
    assert exc.value.attempts_used == 3
    assert calls["find"] == 3
    assert calls["store"] == 0


def test_negative_result_is_failure_and_not_stored(monkeypatch):
    calls = _install(monkeypatch, [-5e-6])
    with pytest.raises(RuntimeError):
        asyncio.run(focus.definite_focus_find_surface(max_retries=1, store_focus=True))
    assert calls["store"] == 0


@pytest.mark.parametrize("store_focus, expected", [(False, None), (True, True)])
def test_loa_wrapper_passes_store_focus(monkeypatch, store_focus, expected):
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return True, 1

    monkeypatch.setattr(focus, "definite_focus_find_surface", fake)
    ok, _, _ = ms.run_definite_focus_find_surface(store_focus=store_focus)
    assert ok is True
    assert seen.get("store_focus") is expected
