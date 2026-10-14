# -*- coding: utf-8 -*-
"""Unit tests for MS_CD7_API_LoA.DefiniteFocusTracker.

The tracker sits on top of run_definite_focus_find_surface (mocked here):
adaptive start below the last surface, one full-range fallback, a
hardware-fault stop after N failed searches in a row, reset on a slide change,
and no full-range search (start −300 µm) with the immersion objective active.
"""
import pytest

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
pytest.importorskip("zen_api", reason="zen_api not resolvable on this machine")

import MS_CD7_API_LoA as ms            # noqa: E402
import MS_zenapi_stage_LM as stage     # noqa: E402

SURFACE = 1.35e-3


class FakeDF:
    """Stand-in for run_definite_focus_find_surface + the Z read-back.

    outcomes: one bool per FindSurface call (True = surface found). Records the
    start Z of every call (None = full range from the −300 µm default).
    """
    def __init__(self, outcomes, surface=SURFACE):
        self.outcomes = list(outcomes)
        self.starts = []
        self.surface = surface

    def find(self, max_retries=3, start_z_m=None):
        self.starts.append(start_z_m)
        ok = self.outcomes.pop(0)
        return (ok, "ok" if ok else "surface not found", 1 if ok else max_retries)


@pytest.fixture
def df(monkeypatch):
    def install(outcomes, objective=3, surface=SURFACE):
        fake = FakeDF(outcomes, surface)
        monkeypatch.setattr(ms, "run_definite_focus_find_surface", fake.find)
        monkeypatch.setattr(ms, "get_current_z_position", lambda: fake.surface)
        monkeypatch.setattr(ms, "get_current_objective_and_optovar", lambda: (objective, 1))
        return fake
    return install


def test_first_search_is_full_range_then_adaptive(df):
    fake = df([True, True])
    t = ms.DefiniteFocusTracker()
    assert t.find_surface() == SURFACE
    assert t.find_surface() == SURFACE
    assert fake.starts == [None, pytest.approx(SURFACE - ms.DF_APPROACH_MARGIN_M)]


def test_seeded_tracker_starts_adaptive(df):
    fake = df([True])
    t = ms.DefiniteFocusTracker(approach_margin_m=50e-6, surface_z_m=1.2e-3)
    t.find_surface()
    assert fake.starts == [pytest.approx(1.2e-3 - 50e-6)]


def test_adaptive_failure_falls_back_to_full_range(df):
    fake = df([False, True])
    t = ms.DefiniteFocusTracker(surface_z_m=SURFACE)
    assert t.find_surface() == SURFACE
    assert fake.starts == [pytest.approx(SURFACE - ms.DF_APPROACH_MARGIN_M), None]
    assert t.consecutive_failures == 0


def test_failed_search_returns_none_and_keeps_last_surface(df):
    df([False, False])
    t = ms.DefiniteFocusTracker(surface_z_m=SURFACE)
    assert t.find_surface() is None
    assert t.consecutive_failures == 1
    assert t.last_surface_z_m == SURFACE


def test_success_clears_the_failure_count(df):
    df([False, False, True])
    t = ms.DefiniteFocusTracker(surface_z_m=SURFACE)
    t.find_surface()
    t.find_surface()
    assert t.consecutive_failures == 0


def test_fault_stop_after_n_failed_searches_in_a_row(df):
    df([False] * 6)   # 3 searches × (adaptive + fallback)
    t = ms.DefiniteFocusTracker(max_consecutive_failures=3, surface_z_m=SURFACE)
    assert t.find_surface() is None
    assert t.find_surface() is None
    with pytest.raises(ms.DefiniteFocusFault, match="3 positions in a row"):
        t.find_surface()


def test_reset_forgets_surface_only(df):
    fake = df([True])
    t = ms.DefiniteFocusTracker(surface_z_m=SURFACE)
    t.consecutive_failures = 2
    t.reset()
    assert t.last_surface_z_m is None and t.consecutive_failures == 2
    t.find_surface()
    assert fake.starts == [None]


def test_immersion_without_known_surface_refuses_full_range(df):
    fake = df([True], objective=stage.IMMERSION_OBJECTIVE_POSITION)
    t = ms.DefiniteFocusTracker()
    with pytest.raises(stage.ImmersionObjectiveActive):
        t.find_surface()
    assert fake.starts == []   # DF never ran


def test_immersion_skips_the_full_range_fallback(df):
    fake = df([False], objective=stage.IMMERSION_OBJECTIVE_POSITION)
    t = ms.DefiniteFocusTracker(surface_z_m=SURFACE)
    assert t.find_surface() is None
    assert fake.starts == [pytest.approx(SURFACE - ms.DF_APPROACH_MARGIN_M)]
    assert t.consecutive_failures == 1


@pytest.mark.parametrize("kwargs", [{"approach_margin_m": -1e-6},
                                    {"max_consecutive_failures": 0},
                                    {"max_retries": 0}])
def test_invalid_arguments(kwargs):
    with pytest.raises(ValueError):
        ms.DefiniteFocusTracker(**kwargs)
