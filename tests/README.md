# SmartMic tests

How to run the SmartMic test suite. For the design rationale (tiers, what each
test asserts, fixtures), see [TEST_PLAN.md](TEST_PLAN.md).

All commands run from the **repo root** (`C:\Users\zeiss\Mike\SmartMic`) in
PowerShell, using the `smartmic` pixi environment.

---

## Two kinds of test

| Kind | Location | Needs the scope? | Default `pytest` |
|------|----------|------------------|------------------|
| **Offline unit** | `tests/unit/` | No | **runs** |
| **Hardware** | `tests/hardware/` | Yes (live ZEN-API + 384 plate) | **skipped** |

Hardware tests are marked `@pytest.mark.hardware` and **only run when you pass
`--run-hardware`**. A plain `pytest` therefore never moves the microscope — it
runs the offline tests and skips the hardware ones. This is the safety guarantee.

---

## 1. Offline unit tests (everyday check, no microscope)

```powershell
pixi run -e smartmic test
```

That is the shortcut for `pytest tests/unit -q` (~2 s, ~130 tests). Variations:

```powershell
pixi run -e smartmic pytest tests/unit -v                    # one line per test
pixi run -e smartmic pytest tests/unit/test_czexp_editor.py  # a single file
pixi run -e smartmic pytest tests/unit -k focus              # name contains "focus"
```

Expect `passed` with no failures. If you only edit library code, this is all you
normally need.

---

## 2. Hardware tests (on the scope)

### Before you start
1. Load the **Multichamber 384** plate in the microscope.
2. ZEN running, ZEN-API gateway up, valid `control-token` in `config.ini`.
3. **Be present** — these tests move the stage, change objectives, drive focus
   toward the sample, and acquire images.

### Run them in safety order (read-only first)

```powershell
# Tier 0 — moves nothing; proves the connection + carrier guard
pixi run -e smartmic pytest tests/hardware/test_readonly.py --run-hardware -v

# Tier 1 — stage / Z / objective moves (auto-restores after each test)
pixi run -e smartmic pytest tests/hardware/test_movement.py --run-hardware -v -x

# Tier 2 — DefiniteFocus + SWAF at 20x/0.95
pixi run -e smartmic pytest tests/hardware/test_focus.py --run-hardware -v -x

# Tier 3 — snap / z-stack / XML acquisitions from bundled .czexp files
pixi run -e smartmic pytest tests/hardware/test_acquisition.py --run-hardware -v -x

# Tier 4 — full end-to-end chain at one well
pixi run -e smartmic pytest tests/hardware/test_smoke_e2e.py --run-hardware -v
```

Once you trust all of them, run the whole hardware suite at once:

```powershell
pixi run -e smartmic test-hw          # = pytest tests/hardware -q --run-hardware
```

If the wrong carrier is loaded or ZEN is unreachable, the hardware tests **skip**
(with a clear reason) rather than fail.

---

## Useful flags

| Flag | Effect |
|------|--------|
| `--run-hardware` | **required** to actually run hardware tests (else skipped) |
| `-v` | verbose: one line per test with PASS/FAIL |
| `-x` | stop at the first failure (recommended for hardware) |
| `-k "expr"` | run only tests whose name matches `expr` |
| `--lf` | re-run only the tests that failed last time |
| `--carrier "Name"` | override the expected carrier (default `Multichamber 384`) |
| `--out-dir "PATH"` | where acquired CZIs are kept (default `F:\UserData\mike\api\test_output`) |
| `--clean-images` | discard acquired CZIs into an ephemeral temp dir instead of keeping them in `--out-dir` |

---

## Where acquired images go

By default, Tier 3/4 write their CZIs to a single persistent folder, kept for
inspection:

```
F:\UserData\mike\api\test_output
```

Override it with `--out-dir "D:\some\path"`. Filenames carry a per-run tag (a
short id unique to each `pytest` invocation), so files never overwrite each other
across runs. Pass `--clean-images` to write to an ephemeral temp dir that is
discarded after the run instead. The folder accumulates across runs — clear it
yourself when you no longer need the images.

---

## Tuning tolerances after the first scope run

Hardware assertions check plausibility and round-trip consistency within named
tolerances, not exact µm. The starting values live in
[`conftest.py`](conftest.py):

```python
STAGE_TOL_M     = 5e-6   # stage XY read-back vs. commanded
Z_TOL_M         = 5e-6   # Z read-back vs. commanded
DF_REPEAT_TOL_M = 9e-6   # two DefiniteFocus FindSurface results agree within
```

Optics constants (also in `conftest.py`):

```python
SAFE_OBJ, SAFE_OPT   = 2, 2   # 5x / 1x optovar — movement test
FOCUS_OBJ, FOCUS_OPT = 3, 1   # 20x/0.95, 2x optovar — focus/SWAF/e2e tiers
```

If a movement/focus test fails only by a small margin, that's a tolerance to
adjust here — not necessarily a real bug.

---

## Layout

```
tests/
├── conftest.py            # --run-hardware gate, carrier guard, snapshot/restore, fixtures
├── pytest.ini             # (at repo root) markers + test discovery
├── README.md              # this file
├── TEST_PLAN.md           # design rationale & coverage
├── unit/                  # offline tests (no scope)
├── hardware/              # @pytest.mark.hardware (opt-in)
└── fixtures/
    ├── czexp/             # experiment + position files
    └── czi/               # sample images for focus-scoring tests
```

## pixi task summary

| Task | Command | Use |
|------|---------|-----|
| `test` | `pytest tests/unit -q` | offline, everyday |
| `test-hw` | `pytest tests/hardware -q --run-hardware` | full hardware suite |
| `test-all` | `pytest -q --run-hardware --cov` | everything + coverage |
