# -*- coding: utf-8 -*-
"""
Shared pytest configuration and fixtures for the SmartMic test suite.

Two tiers (see tests/TEST_PLAN.md):

* **Offline unit tests** (``tests/unit/``) — no microscope, run anywhere.
* **Hardware tests** (``tests/hardware/``) — talk to the live ZEN-API gateway.
  They are marked ``@pytest.mark.hardware`` and are SKIPPED unless ``--run-hardware``
  is given AND the expected sample carrier is loaded. This makes a plain ``pytest``
  safe: it never moves hardware.

This conftest also puts the repo root on ``sys.path`` so the test modules can
``import MS_*`` / ``import zeiss_paths`` regardless of the working directory.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# --------------------------------------------------------------------------
# Make the repo-root modules importable (MS_*, zeiss_paths).  This conftest
# lives in tests/, so the repo root is its parent.
# --------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# --------------------------------------------------------------------------
# Hardware-test configuration constants (see TEST_PLAN.md §5).
# --------------------------------------------------------------------------
DEFAULT_CARRIER = "Multichamber 384"

# Optics — two configs:
#   SAFE_*  : 5x / 1x optovar — lowest-risk, used by the objective-change move test.
#   FOCUS_* : 20x/0.95, 2x optovar — the bundled SWAF experiments are tuned for it.
SAFE_OBJ, SAFE_OPT = 2, 2
FOCUS_OBJ, FOCUS_OPT = 3, 1

# Tolerances (metres).  Tune after the first real run.
STAGE_TOL_M = 5e-6
Z_TOL_M = 5e-6
DF_REPEAT_TOL_M = 9e-6


# --------------------------------------------------------------------------
# CLI options / gating
# --------------------------------------------------------------------------
def pytest_addoption(parser):
    parser.addoption(
        "--run-hardware",
        action="store_true",
        default=False,
        help="Run the hardware tests against the live ZEN-API microscope.",
    )
    parser.addoption(
        "--carrier",
        action="store",
        default=DEFAULT_CARRIER,
        help="Expected sample-carrier name for the hardware tests.",
    )
    parser.addoption(
        "--clean-images",
        action="store_true",
        default=False,
        help="Discard CZIs acquired by the hardware tests into an ephemeral temp "
        "dir (default: keep them in --out-dir for inspection).",
    )
    parser.addoption(
        "--out-dir",
        action="store",
        default=r"F:\UserData\mike\api\test_output",
        help="Persistent folder for CZIs acquired by the hardware tests "
        "(kept for inspection). On the same drive as ZEN's default output so the "
        "post-acquisition move stays fast. Ignored when --clean-images is given.",
    )


def pytest_collection_modifyitems(config, items):
    """Skip hardware-marked tests unless --run-hardware was given."""
    if config.getoption("--run-hardware"):
        return
    skip_hw = pytest.mark.skip(reason="hardware test (needs --run-hardware)")
    for item in items:
        if "hardware" in item.keywords:
            item.add_marker(skip_hw)


# --------------------------------------------------------------------------
# Fixture-path fixtures (used by both unit and hardware tests)
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def czexp_dir() -> Path:
    return FIXTURES_DIR / "czexp"


@pytest.fixture(scope="session")
def czi_dir() -> Path:
    return FIXTURES_DIR / "czi"


@pytest.fixture(scope="session")
def keep_images(request) -> bool:
    """True when acquired CZIs should be kept (the default)."""
    return not request.config.getoption("--clean-images")


@pytest.fixture
def out_dir(request, tmp_path, keep_images) -> Path:
    """Output folder for CZIs acquired by the hardware tests.

    Default: the persistent ``--out-dir`` folder, kept for inspection. With
    ``--clean-images``: an ephemeral pytest temp dir that is discarded after the
    run. Filenames carry a per-run tag (see ``run_tag``) so files never collide,
    even across runs sharing the persistent folder.
    """
    if keep_images:
        d = Path(request.config.getoption("--out-dir"))
    else:
        d = tmp_path / "acquired"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture(scope="session")
def run_tag() -> str:
    """A short id unique to this pytest run.

    Used to make acquisition output names unique per run. ZEN enforces output-name
    uniqueness in its default output folder, so a fixed name left over from an
    earlier (e.g. interrupted) run causes ALREADY_EXISTS; a per-run tag avoids it.
    """
    import uuid
    return uuid.uuid4().hex[:8]


# --------------------------------------------------------------------------
# Hardware fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def hw(request) -> SimpleNamespace:
    """Hardware-test configuration constants (carrier, optics, tolerances)."""
    return SimpleNamespace(
        carrier=request.config.getoption("--carrier"),
        safe_obj=SAFE_OBJ,
        safe_opt=SAFE_OPT,
        focus_obj=FOCUS_OBJ,
        focus_opt=FOCUS_OPT,
        stage_tol_m=STAGE_TOL_M,
        z_tol_m=Z_TOL_M,
        df_repeat_tol_m=DF_REPEAT_TOL_M,
    )


@pytest.fixture(scope="session")
def scope(request, hw):
    """Session fixture for hardware tests: verify carrier, snapshot, restore.

    * Skips the entire hardware run (with a clear message) if the expected
      sample carrier is not loaded — protects against driving the stage to
      wrong/unsafe positions for a different plate.
    * Snapshots the current XY / Z / objective / optovar before any test runs
      and restores them on teardown, even if a test fails.

    Yields the imported ``MS_CD7_API_LoA`` module so hardware tests can call the
    high-level API as ``scope.get_current_z_position()`` etc.
    """
    import zeiss_paths  # noqa: F401  — extends sys.path so zen_api resolves
    pytest.importorskip("zen_api", reason="zen_api not resolvable on this machine")
    import MS_CD7_API_LoA as ms

    # --- carrier guard ---
    try:
        carrier = ms.get_sample_carrier_name()
    except Exception as e:  # connection / gateway error
        pytest.skip(f"Could not query sample carrier from ZEN: {e}")
    if carrier != hw.carrier:
        pytest.skip(f"Wrong sample carrier loaded: {carrier!r} (expected {hw.carrier!r})")

    # --- snapshot state ---
    snap = {}
    try:
        snap["xy"] = ms.get_current_xy_stage_position()
        snap["z"] = ms.get_current_z_position()
        snap["optics"] = ms.get_current_objective_and_optovar()
    except Exception as e:
        pytest.skip(f"Could not snapshot microscope state: {e}")

    try:
        yield ms
    finally:
        _restore_scope(ms, snap)


def _restore_scope(ms, snap):
    """Best-effort restore of optics → XY → Z; never raise (don't mask results)."""
    try:
        obj, opt = snap["optics"]
        ms.set_objective_set_optovar_sync(obj, opt)
        ms.move_stage_to_new_xy_position(snap["xy"][0], snap["xy"][1])
        ms.move_focus_to_new_z_position(snap["z"])
    except Exception as e:
        print(f"[conftest] WARNING: could not fully restore scope state: {e}")


@pytest.fixture
def restore_state(scope):
    """Function-scoped: snapshot XY/Z/optics, run the test, then restore them.

    Wrap each motion/focus test with this so a failure in one test doesn't leave
    the scope in a state that poisons the next.
    """
    snap = {
        "xy": scope.get_current_xy_stage_position(),
        "z": scope.get_current_z_position(),
        "optics": scope.get_current_objective_and_optovar(),
    }
    try:
        yield scope
    finally:
        _restore_scope(scope, snap)
