# -*- coding: utf-8 -*-
"""Unit tests for MS_CD7_API_LoA.set_optics_verified (change → verify → settle).

The changer call and the read-back are mocked; time is faked so the polling /
settle / timeout logic runs instantly.
"""
import pytest

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
pytest.importorskip("zen_api", reason="zen_api not resolvable on this machine")

import MS_CD7_API_LoA as ms   # noqa: E402

TARGET = (3, 1)


@pytest.fixture
def clock(monkeypatch):
    """Fake monotonic clock advanced by time.sleep; records the sleeps."""
    state = {"now": 0.0, "sleeps": []}

    def sleep(s):
        state["sleeps"].append(s)
        state["now"] += s

    monkeypatch.setattr(ms.time, "sleep", sleep)
    monkeypatch.setattr(ms.time, "monotonic", lambda: state["now"])
    return state


def _optics(monkeypatch, readbacks, change_ok=True):
    """Mock the change (records calls) and a sequence of read-backs (last repeats)."""
    seq = list(readbacks)
    calls = []

    def change(obj, opt):
        calls.append((obj, opt))
        return change_ok

    def read():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(ms, "set_objective_set_optovar_sync", change)
    monkeypatch.setattr(ms, "get_current_objective_and_optovar", read)
    return calls


def test_confirmed_and_settled(monkeypatch, clock):
    calls = _optics(monkeypatch, [TARGET])
    ms.set_optics_verified(*TARGET, settle_s=5.0)
    assert calls == [TARGET]
    assert clock["sleeps"] == [5.0]


def test_waits_for_the_read_back(monkeypatch, clock):
    _optics(monkeypatch, [(2, 2), (2, 2), TARGET])
    ms.set_optics_verified(*TARGET, settle_s=5.0, poll_s=2.0)
    assert clock["sleeps"] == [2.0, 2.0, 5.0]


def test_changer_timeout_is_not_fatal_if_read_back_settles(monkeypatch, clock):
    _optics(monkeypatch, [TARGET], change_ok=False)
    ms.set_optics_verified(*TARGET, settle_s=1.0)


def test_never_settles_raises(monkeypatch, clock):
    _optics(monkeypatch, [(2, 2)])
    with pytest.raises(RuntimeError, match="never settled"):
        ms.set_optics_verified(*TARGET, settle_s=1.0, timeout_s=10.0, poll_s=2.0)
    assert clock["now"] >= 10.0


def test_flapping_after_settle_keeps_waiting(monkeypatch, clock):
    # target → (settle) → changed again → target → (settle) → target: confirmed.
    _optics(monkeypatch, [TARGET, (2, 2), TARGET, TARGET])
    ms.set_optics_verified(*TARGET, settle_s=5.0, poll_s=2.0)
    assert clock["sleeps"] == [5.0, 2.0, 5.0]


def test_invalid_position_raises_before_any_change():
    with pytest.raises(ValueError):
        ms.set_optics_verified(99, 1)


def test_negative_timing_raises():
    with pytest.raises(ValueError):
        ms.set_optics_verified(*TARGET, settle_s=-1.0)
