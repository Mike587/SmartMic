# SmartMic — Test Plan (DRAFT for approval)

Status: **Complete and fully validated.** Offline tier green — 92 passing at the
time of this plan (`pixi run -e smartmic test`), now 129 as preflight /
stage-motion / immersion-guard coverage was added afterward (see §3). Hardware
tiers 0–4 validated on the live scope (Multichamber 384, 20×/0.95) — 23 passing
in one run (`pixi run -e smartmic test-hw`, ~6.5 min), unchanged since. Three
library robustness bugs were found and fixed along the way (see §12). Remaining:
optional follow-ups in §12 / Deferred.

Author of plan: assistant, 2026-06-22. Reviewed by: _pending_.

---

## 1. Goals & guiding principles

1. **Protect the pure logic cheaply.** Most regressions (XML editing, position
   parsing, validation, wrapper orchestration) can be caught with fast tests that
   need no microscope and run anywhere.
2. **Exercise the real hardware deliberately.** The `MS_zenapi_*` gRPC layer only
   has meaning against the actual scope. Those tests are **opt-in**, run on the
   scope machine with a known plate, and are gated so a plain `pytest` never moves
   hardware by accident.
3. **Assert plausibility, not exact values.** Focus/stage readings vary run-to-run.
   Hardware tests assert success, in-range values, round-trip consistency, and
   geometry — never hard-coded µm.
4. **Always leave the scope as we found it.** A session fixture snapshots XY / Z /
   objective / optovar and restores them on teardown, even on failure.
5. **Reuse what exists.** `verify_zen_api.py` already is a read-only smoke test;
   tier 0 formalizes it into pytest. The existing carrier-guard pattern from the
   PoC becomes the gate.

`pytest` (+ `pytest-cov`) is already in `pixi.toml`, so no new framework.

---

## 2. Test tiers

Tiers run lowest-risk first. A failure in a lower tier skips the higher ones.

| Tier | Name | Hardware? | Default `pytest`? | Risk |
|----|------|-----------|-------------------|------|
| U  | Offline unit | No | **Yes (runs)** | none |
| 0  | HW read-only | Yes | skipped | none — moves nothing |
| 1  | HW movement | Yes | skipped | stage/Z/objective motion |
| 2  | HW focus search | Yes | skipped | focus drives toward sample |
| 3  | HW acquisition | Yes | skipped | runs experiments, writes CZIs |
| 4  | HW end-to-end smoke | Yes | skipped | full PoC chain at one well |

Tiers 0–4 only execute with `--run-hardware` **and** a passing carrier check.

---

## 3. Tier U — offline unit tests (no scope, CI-safe)

These are the bulk of the value. Grouped by module.

### `tests/unit/test_czexp_editor.py` — `MS_czexp_editor.py` (pure stdlib)
- `load_czexp` / `save_czexp` round-trip: load a fixture, save, reload, assert
  structural equality + that the file is UTF-8-with-BOM + has the XML declaration.
- `set_position` / `add_single_tile_region`: set X/Y/Z (metres in, µm in file),
  reload, assert the written µm values.
- `set_zstack_range`: set first/last/interval (m), assert via `get_zstack_interval_m`
  and the First/Last elements.
- `fit_lsm_crop`: assert frame/zoom math at constant pixel size and at a fixed frame.
  (The `target_fov_um <= 0` → `ValueError` guard added alongside the other
  2026-07-15 fixes is not covered here — a gap.)
- `set_tile_region_center` / `clear_single_tile_regions` (returns count) /
  `find_stitch_regions` / `find_tile_regions` on a stitch fixture.
- `is_stitching_configured`: true for a valid stitch, false for the empty-`NULL`
  remote-processing placeholder case (the one that would abort an API run).
- `get_lsm_pixel_size_um`, `get_zstack_interval_m`, `summarize` value checks.
- `set_run_mode_lock`, `set_lsm_scan_speed_max`, `set_lsm_sampling_mode_user`:
  assert the flags are flipped in the reloaded XML.
- Helpers: `_fmt_float`, `_parse_pair`, `_require` (raises on missing tag).

### `tests/unit/test_helper.py` — `MS_Helper_function.py`
- `load_positions_from_czexp`: parse the positions fixture; assert well names,
  position counts, µm→m conversion, and the **`IsUsedForAcquisition` default-True**
  rule (a position with the tag missing is treated as used).
- `setup_run_logger`: creates a timestamped log file, returns `(logger, path)`
  (the return-shape that TODO.md already corrected), file exists, second call
  doesn't duplicate handlers.
- `compute_focus_score`: sharp-CZI score **>** blurry-CZI score; returns a float;
  `None`/graceful on a non-CZI path.
- `get_focus_position_from_czi`: extracts the embedded `FocusPosition` (µm) from a
  fixture CZI; `None` when absent.
- `get_zstack_z_range`: plane count, step, first/center/last from a z-stack CZI.

### `tests/unit/test_validation.py` — `MS_CD7_API_LoA` validators (pure)
- `validate_objective_number` / `validate_optovar_number`: in-range, out-of-range,
  non-int → `(False, msg)`; boundaries (1, 4 / 1, 3).
- `validate_z_position`: numeric check + the ±0.01 m safe range boundaries.
- `validate_xy_position`: numeric + the stage-limit boundaries (X 0–0.12, Y 0–0.09).

### `tests/unit/test_loa_wrappers.py` — sync wrappers with the async layer **mocked**
Monkeypatch the `MS_zenapi_*` async functions so we test orchestration without a scope:
- `run_definite_focus_find_surface`: success path returns `(True, msg, attempts)`.
  The async fn never returns a clean failure — it raises after exhausting
  retries, with the real attempt count attached as `.attempts_used`. A caught
  exception carrying that attribute reports the real count; one without it
  (an unrelated/unexpected exception) falls back to `max_retries`.
- `set_objective_set_optovar_sync`: validates inputs first (raises before any
  async call); happy path confirms on the first poll against a mocked
  `get_current_objective_and_optovar`. (The retry loop and the immersion
  timeout selection inside `_set_and_wait` are not exercised by this file —
  a coverage gap, not a passing test.)
- `run_experiment`: `do_snap_and_live=False` routes to `run_experiment_by_name`,
  `True` routes to `check_experiment_api` (assert which mock was called).
- `move_focus_to_new_z_position` / `move_stage_to_new_xy_position`: raise on invalid
  input *before* any async call (assert the async mock is never awaited).
- `preflight`: happy path sets stage motion and logs no errors; wrong carrier /
  busy scope / gateway-down each abort (`False`) before stage motion is touched;
  a `set_stage_motion_sync` result with `speed_x/y = None` (gateway rejects
  `SetSpeed`, see `test_stage_motion.py`) still passes, with an info log noting
  the throttle wasn't supported. *(Added after this plan was written — see
  `MS_CD7_API_LoA.preflight`.)*

### `tests/unit/test_stage_guard.py` — immersion guard in `MS_zenapi_stage_LM`
*(Added after this plan was written.)* `move_stage_to_new_xy_position` lowers Z
to 0 for collision safety before every XY move, which would break the
immersion bridge if the 50x immersion objective is active. Mocked gRPC layer:
- Immersion objective active → raises before the Z=0 lower runs (`calls["z"] == 0`).
- Dry objective active → no raise; the normal Z=0 lower runs once.

### `tests/unit/test_stage_motion.py` — `set_stage_motion` in `MS_zenapi_stage_LM`
*(Added after this plan was written.)* Mocked gRPC layer:
- Explicit percents / `None` defaults: both `set_speed` and `set_acceleration`
  issued once per axis with the expected values; the returned dict reflects them.
- Confirmed-on-hardware (2026-07-14) asymmetry: a `SetSpeed`/`GetSpeed` rejection
  is tolerated (`speed_x/y` come back `None`) since acceleration is the parameter
  that actually protects a live sample; an acceleration failure DOES propagate.

### `tests/unit/test_zenapi_helpers.py` — `MS_zenapi_helpers.py`
- `get_objective_by_position` / `get_optovar_by_position` / `get_used_*_positions`:
  feed duck-typed fake objects (no `zen_api` needed), assert lookups.
- `set_logging`: returns the shared `"smartmic"` logger; idempotent (no duplicate
  handlers on repeated calls).
- `open_zen_channel`: with a mock channel, assert `.close()` is called on **both**
  normal exit and when the body raises (TODO.md says this was hand-verified once;
  this makes it a permanent regression test).

### `tests/unit/test_image_analysis.py` — `MS_image_analysis.run_analysis`
- Point it at a trivial dummy analysis script (a fixture) that writes a known
  output; assert the subprocess runs, `PIXI_*` env is stripped, and the success
  bool reflects the child's exit code (success + failure cases).

> **Coverage note:** every public function in the repo is referenced above except
> the genuinely hardware-only `MS_zenapi_*` gRPC calls and the CLI `main()` demos,
> which are covered by the hardware tiers / are out of scope (see §7).

---

## 4. Tiers 0–4 — hardware tests (on the scope, 384 plate loaded)

All carry `@pytest.mark.hardware`. Tolerances are named constants in `conftest.py`,
tunable after a first real run (proposed starting values below — please sanity-check).

**Standalone-files principle.** Tests never assume an experiment already exists in
ZEN's stored library — another machine with the same scope won't have the same named
experiments, so a name-based run is not portable. Every experiment a test runs comes
from a **`.czexp` (or its XML) bundled in `tests/fixtures/`**, executed via the
from-path / from-xml entry points: `run_experiment_from_path`,
`run_experiment_from_xml`, and `run_swaf_from_path` (added 2026-06-22 — see §11). The
remaining name-only entry points (`run_experiment` by name and `check_experiment_api`)
get special handling: see the run-by-name note (Tier 3).

### Tier 0 — `tests/hardware/test_readonly.py` (moves nothing)
Pytest-ified `verify_zen_api.py`:
- `get_sample_carrier_name` == expected; `get_sample_carrier_info` dict has the
  expected keys/shape.
- `get_current_objective_and_optovar` returns ints in valid ranges;
  `get_current_objective_and_optovar_names` returns `((name,pos),(name,pos))`.
- `get_current_xy_stage_position` / `get_current_z_position` return floats in range.
- `is_microscope_busy` is a bool; `get_running_experiment_status` is a dict or `None`.

### Tier 1 — `tests/hardware/test_movement.py`
Each test is wrapped by a function-scoped snapshot/restore fixture so one failure
doesn't poison the next.
- **Stage round-trip:** read XY → move to a safe well center taken from the
  positions fixture → read back within `STAGE_TOL_M` → return to start.
- **Z round-trip:** read Z → `move_focus_to_new_z_position(z + small Δ)` → read back
  within `Z_TOL_M` → return.
- **Objective/optovar:** `set_objective_set_optovar_sync(SAFE_OBJ, SAFE_OPT)` →
  `get_current_objective_and_optovar` confirms → restore.
- **Guard tests (still no risky motion):** invalid objective/Z/XY raise before any
  motion (mirrors the unit guard, confirmed against the real validators).

### Tier 2 — `tests/hardware/test_focus.py`
Requires the **focus optics** set first: `FOCUS_OBJ = 3`, `FOCUS_OPT = 1`
(Plan-Apochromat **20×/0.95**, 2× optovar). This is *not* the 5× `SAFE_OBJ/SAFE_OPT`
used by the movement tier — the bundled SWAF experiments (`swaf_coarse_20x` /
`swaf_fine_20x`) are tuned for the 20× objective, so the fixture sets 20×/0.95 before
any DF/SWAF here. (20×/0.95 is high-NA with a short working distance; DF FindSurface
drives toward the sample, exactly as the PoC does — adaptive start-Z keeps it safe.)
- **DF FindSurface:** succeeds; resulting Z within the safe range; `attempts` ≥ 1.
- **DF repeatability:** two consecutive FindSurface calls agree within `DF_REPEAT_TOL_M`
  (a real regression signal for focus drift/instability).
- **DF recall:** after a FindSurface+store, `run_definite_focus_recall` returns a
  Z (µm) close to the stored surface.
- **SWAF (from path):** `run_swaf_from_path(<swaf_coarse_20x.czexp>)` (and
  `swaf_fine_20x`) returns a focus position within range; `None`-handling path is
  exercised if a SWAF legitimately fails. Self-contained — loaded from the bundled
  `.czexp`, not from a name in ZEN's library (resolved 2026-06-22; see §11).

### Tier 3 — `tests/hardware/test_acquisition.py`
Writes to a per-test output folder and **keeps the produced CZIs for inspection** by
default (`--clean-images` deletes them after asserting). Every experiment is run from
a **bundled standalone `.czexp` / XML** — never a name assumed to be in ZEN's library
— so the suite is portable to any machine with the scope.
- **Snap (by path):** `run_experiment_from_path(<snap.czexp>, out, name)` → result
  dict has `exp_result_path`; the CZI exists and is non-empty.
- **Z-stack (by path):** `run_experiment_from_path(<zstack.czexp>, …)` → CZI exists;
  `get_zstack_z_range` reports the expected plane count / step from the czexp.
- **By XML:** read a bundled `.czexp`'s text and feed it to
  `run_experiment_from_xml(<xml>, …)` → CZI exists. Self-contained — the XML comes
  from the repo, not from ZEN.
- **Result handling:** assert the result was moved out of ZEN's default folder and
  the collision counter produces a fresh name when the target already exists
  (the `_move_result_and_cleanup` / `_unique_czi_name` regression from TODO.md).
- **Status:** `get_running_experiment_status` is non-`None` during a run (if timing
  allows) and `None` when idle.
- **Run-by-name — deliberately *not* in the portable suite.** `run_experiment(name)`
  and the `check_experiment_api` smoke test resolve an experiment by *name* from ZEN's
  stored library, which another machine won't have. The by-path and by-XML tests
  above cover the same load→run→collect tail from repo-owned files instead. An
  optional `@pytest.mark.local` test can still exercise run-by-name on a machine where
  the named experiment exists — skipped by default.

### Tier 4 — `tests/hardware/test_smoke_e2e.py` — the "overall test"
One well, the full PoC chain hardened into a single test, **with the external
nuclei analysis stubbed** (a fixture script that emits a fixed `targets.json`) so the
test is deterministic and doesn't depend on the image-analysis env:
move stage → set optics → DF FindSurface → SWAF coarse+fine → acquire overview →
(stubbed analysis) → move to one fake nucleus → DF/SWAF → acquire single plane →
acquire small z-stack → assert each stage produced its artifact. This is the
integration test that proves the wrappers compose correctly end-to-end.

All acquisitions here use the **bundled `.czexp` files** (via `run_experiment_from_path`),
not named experiments — same portability rule as Tier 3. SWAF runs the bundled
`swaf_coarse_20x` / `swaf_fine_20x` via `run_swaf_from_path` with the 20×/0.95 focus
optics set (FOCUS_OBJ/FOCUS_OPT; see Tier 2).

---

## 5. Gating & shared infrastructure (`tests/conftest.py`)

- `pytest_addoption`: `--run-hardware` (off by default), `--carrier=<name>`
  (override expected carrier), `--out-dir=<path>` (persistent folder for acquired
  CZIs, default `F:\UserData\mike\api\test_output`, same drive as ZEN's output),
  `--clean-images` (write to an ephemeral temp dir instead of keeping them).
- Marker `hardware`; `pytest_collection_modifyitems` auto-skips `hardware` tests
  unless `--run-hardware` is given.
- **Session fixture `scope` (hardware only):** assert carrier == expected (skip the
  whole hardware run with a clear message if wrong/absent); snapshot XY/Z/objective/
  optovar; `yield`; restore in a `finally`.
- **Function fixture `restore_state`:** snapshot+restore around each motion test.
- All hardware tests log via `setup_run_logger`, so each run drops a per-run log
  artifact under the test output folder for post-mortem.
- Proposed tolerance constants (please confirm against real hardware behavior):
  `STAGE_TOL_M = 5e-6`, `Z_TOL_M = 5e-6`, `DF_REPEAT_TOL_M = 9e-6`.
- Optics constants — two configs:
  - `SAFE_OBJ = 2`, `SAFE_OPT = 2` (5× / 1× optovar) — used by the movement tier's
    objective-change exercise: low mag, large working distance, lowest risk.
  - `FOCUS_OBJ = 3`, `FOCUS_OPT = 1` (Plan-Apochromat 20×/0.95, 2× optovar) — set
    before the focus tier (Tier 2) and the e2e smoke (Tier 4), because the bundled
    `swaf_*_20x` experiments are tuned for the 20× objective.

---

## 6. Fixtures (`tests/fixtures/`, git-tracked)

Keep them small so they're comfortable in git. ✅ = present, ⏳ = still to add.

### `tests/fixtures/czexp/`
1. ✅ **`positions_384.czexp`** — positions file for the loaded 384 plate (several
   wells, mix of used/unused). Drives the offline position-parsing test **and** the
   safe stage targets for tier 1. Must match the real plate so the movement test goes
   to valid wells.
2. ✅ **`snap_single.czexp`** — minimal single-plane snap. Run by path (self-contained).
3. ✅ **`zstack_small.czexp`** — a tiny z-stack (a few planes) for the acquisition +
   z-range geometry tests. Run by path.
4. ✅ **`swaf_coarse_20x.czexp`** + **`swaf_fine_20x.czexp`** — the SWAF experiments,
   **tuned for the 20×/0.95 objective** (so the focus tier sets `FOCUS_OBJ/FOCUS_OPT`
   = pos 3/1 before running them). Loaded **by path** via `run_swaf_from_path` — no
   dependence on ZEN's stored library (see §11).
5. ✅ *(editor coverage)* **`stitch_region.czexp`** — has a `TileRegion`/stitch +
   processing step, so `find_stitch_regions`, `set_tile_region_center`,
   `clear_single_tile_regions`, and `is_stitching_configured` are exercised.
6. ✅ *(confocal LSM coverage)* **`zstack_LSM.czexp`** — a real confocal experiment
   with a full LSM detector (Zoom / FrameSize / Sampling / SamplingMode=Confocal /
   ScaledImageRectangleSize …) + a `ZStackSetup`. The other fixtures are widefield, so
   this is what exercises `find_lsm_detector`, `get_lsm_pixel_size_um`, `fit_lsm_crop`,
   `summarize`, `set_lsm_scan_speed_max`, `set_lsm_sampling_mode_user`. Added 2026-06-22.

### `tests/fixtures/czi/`
7. ✅ **`sharp_small.czi`** — an in-focus single-plane image (high focus score). ~7.7 MB.
8. ✅ **`blurry_small.czi`** — an out-of-focus single-plane image (low focus score). ~7.7 MB.
   Together they prove `compute_focus_score` orders sharpness correctly.
9. ✅ **`zstack.czi`** — a small z-stack with an embedded `FocusPosition`, for
   `get_zstack_z_range` and best-plane scoring. ~9.3 MB.

> Space: switched the focus-scoring fixtures to the `*_small` variants (~7.7 MB vs
> ~25 MB each, ~35 MB saved). The original `sharp.czi` / `blurry.czi` are superseded
> and should be deleted (they were locked by another process at switch time).

> **Git policy (decided):** fixtures are committed directly to the repo — no git-lfs.
> Accept the modest clutter as the price of reproducible tests; keep CZIs cropped/binned
> small where practical to limit repo bloat.

I'll supply the dummy analysis-stub script and any synthetic/hand-edited czexp
needed for negative cases (e.g. the empty-`NULL` stitch placeholder) myself.

---

## 7. Out of scope 

- CLI `main()` demos in `MS_zenapi_objectivechanger` / `swaf` / `verify_zen_api`
  (interactive/demo; the underlying functions are covered).
- Local-only `projects/` (`HD_Nuclei_from_slide`, `Marc_SM`) and `sandbox/` — they're
  git-ignored and not part of the shared library. (Easy to add a `Marc_SM`
  `_parse_log` unit test later if you want.)
- Performance/timing assertions (focus sweep durations) — log them, don't gate on them.

---

## 8. `pixi.toml` tasks (added)

```toml
[feature.smartmic.tasks]
test    = "pytest tests/unit -q"                          # offline, CI-safe
test-hw = "pytest tests/hardware -q --run-hardware"       # on the scope, opt-in
test-all = "pytest -q --run-hardware --cov"               # everything + coverage
```

---

## 9. Build order

1. ✅ Scaffold `tests/` tree + `conftest.py` gate + `pytest.ini` + pixi tasks.
2. ✅ Tier U unit tests against the fixtures — **92 passing** (`pixi run -e smartmic test`).
3. ✅ Tier 0 read-only (`test_readonly.py`) — validated on scope (8/8).
4. ✅ Tiers 1–3 (`test_movement` / `test_focus` / `test_acquisition`) — validated on
   scope. Current tolerances (`STAGE_TOL_M`/`Z_TOL_M`/`DF_REPEAT_TOL_M`) held; no
   tuning needed on this plate.
5. ✅ Tier 4 end-to-end smoke (`test_smoke_e2e.py`) — validated on scope.

All 23 hardware tests pass in one run (`pixi run -e smartmic test-hw`, ~6.5 min on
the Multichamber 384 / 20×/0.95).

---

## 10. Decisions (resolved) & remaining items

Resolved with the user (2026-06-22):
1. **Safe optics:** `SAFE_OBJ = 2`, `SAFE_OPT = 2` (5× / 1× optovar — dry, low risk).
2. **No reliance on named experiments in ZEN.** Tests run bundled standalone `.czexp`
   files by path/XML. SWAF reuses `DV_001_swaf_00{1,2}`, with **local copies committed**
   to the repo as source of truth (it still loads by name — Tier 2 caveat).
3. **Tolerances:** `STAGE_TOL_M = 5e-6`, `Z_TOL_M = 5e-6`, `DF_REPEAT_TOL_M = 9e-6`
   (user-set; tune after first real run).
4. **Fixtures committed directly to git** (no git-lfs); keep CZIs small where practical.
5. **Acquisition tests keep their CZIs** for inspection by default (`--clean-images`
   to delete).

Remaining / optional (not blocking):
- *(None outstanding.)* The earlier SWAF-by-name caveat has been resolved in code —
  see §11.

---

## 11. Library change log (done before scaffolding)

- **2026-06-22 — SWAF can now run from a path.** `run_software_autofocus` only loaded
  by name, which would have forced the SWAF test to depend on ZEN's stored library
  (not portable). Refactored `MS_zenapi_swaf.py` to mirror the `run_experiment_*`
  family: extracted the search/retry loop into `_find_auto_focus_with_retry`, added
  `run_software_autofocus_from_path(czexp_path, …)` (loads by path), and kept
  `run_software_autofocus` (by name) as a thin delegator. Exposed
  `MS_CD7_API_LoA.run_swaf_from_path(czexp_path, timeout)` alongside `run_swaf`.
  Non-breaking: existing by-name callers (incl. the PoC) are unchanged.

- **2026-06-22 — conftest `scope` fixture import order fix.** The fixture called
  `pytest.importorskip("zen_api")` *before* `import zeiss_paths`, but `zeiss_paths`
  is what puts `zen_api` on `sys.path` — so every hardware test skipped with
  "zen_api not resolvable" even on the scope. Reordered to import `zeiss_paths`
  first. Verified: Tier 0 read-only now passes against the live gateway (8/8).

## 12. On-scope validation findings (2026-06-22)

Running the hardware tiers against the live scope (Multichamber 384, 20×/0.95):

- ✅ **Tier 0 read-only — 8/8 pass.**
- ✅ **Tier 2 focus — fixed (library robustness bug).** `definite_focus_find_surface`'s
  *initial* `move_to(start_z_m)` was at `MS_zenapi_focus.py:108`, **outside** the
  retry/try-except loop. On this Z-drive the default start (−300 µm) is below the
  reachable minimum (the drive bottoms out near −293 µm), so ZEN raised
  `INTERNAL: Requested position was not reached` and the whole DF call aborted
  before any search.
  - **Design intent (per Mike):** DF FindSurface is meant to locate the surface
    from *any* start, so −300 µm is a fine safe start — the exact start position
    need not be reached. Surface Z is plate-dependent (notably the carrier's
    **skirt**), which is why neither the library nor the test should assume a
    particular surface Z. Once a focus is known, starting ~300 µm below the
    previous focus is the faster path (the PoC already does this, with 100 µm).
  - **Fix (done):** wrapped the initial `move_to` in try/except — a start the drive
    can't fully reach is now logged and the search proceeds from wherever it
    landed, instead of aborting. This also fixes the PoC's *first* DF (default
    −300 µm start). `test_focus.py` uses the −300 µm default.
- ℹ️ **ZEN.Error.log during the focus run — WARN only, not serious.** No ERROR/FATAL
  during the run. ~1900 benign `FindCameraOfTrackSetup … no Camera` warnings (normal
  for confocal/LSM tracks — no camera). ~21 `Command not allowed while device in
  motion (0x0D)` / async-timeout warnings on `MTBFocusStabilizerLiveCellScanner`
  (the DF hardware): transient races from firing DF/Z/objective commands back-to-back
  faster than interactive use; ZEN retried and recovered (test passed). Low-priority
  follow-up *if they ever become failures*: a small settle delay between rapid Z/DF
  moves, or tolerate/back-off on 0x0D in the focus wrapper.

- ✅ **Tier 3 acquisition — output-name collision (test fixed; library gap noted).**
  `run_experiment_from_path(..., "test_snap")` failed with
  `ALREADY_EXISTS: An output with the same name already exists`. ZEN writes to its
  **default** output folder (`F:\UserData\mike\temp`) and enforces name-uniqueness
  *there*; a stale `test_snap.czi` from an earlier interrupted run blocked it.
  - **Test fix (done):** acquisition/e2e tests now append a per-run tag
    (`run_tag`, a session uuid) to output names, so they never collide across runs.
    A successful run also sweeps the orphan (`_move_result_and_cleanup` clears the
    default folder).
  - **Library gap for Mike (not changed):** `_unique_czi_name` makes the name unique
    against the *custom* output folder, but ZEN enforces uniqueness in its *default*
    folder — so the built-in collision counter can't prevent `ALREADY_EXISTS` from a
    stale file in the default folder. Low severity (PoC uses position-unique names +
    the default folder is swept each run), but the counter checks the wrong folder.
    Fix option: make the name unique against the default folder (or both).
- ✅ **Tier 3 acquisition — cross-drive move (library fixed).** After acquiring,
  `_move_result_and_cleanup` moved the CZI from ZEN's default folder (`F:\…`) to the
  custom folder with `Path.rename`, which fails across drives on Windows
  (`WinError 17`) — the test's output folder was on `C:\`. Fixed: use `shutil.move`
  (same-drive → identical to rename; cross-drive → copy+delete) in both
  `_move_result_and_cleanup` and the snap move in `check_experiment_api`. Safe,
  behavior-preserving on same-drive, and matches the documented contract that
  `custom_folder` may be any path. Production (PoC writes to `F:\…`, same drive as
  ZEN's temp) was unaffected either way.

### Deferred — NOT to be worked on now (noted for later)

Per the 2026-06-22 decision, leave the PoC alone for the moment. Recorded so it
isn't lost:
- **Migrate the PoC's SWAF calls to `run_swaf_from_path`** (currently
  `ms.run_swaf("DV_001_swaf_001")` etc. in `MS_SmartMic_PoC.py:219`) so the
  production pipeline is also free of named-experiment dependencies.
- **Same wart for the PoC's experiment acquisitions** — `ms.run_experiment("DAPI_GFP_001", …)`
  loads by name. If full PoC portability is wanted, switch these to
  `run_experiment_from_path` against committed `.czexp` files. Bigger change; treat
  separately from the SWAF item.
- Decide where the PoC's canonical `.czexp` files should live if/when migrated
  (repo vs. machine-local). --> in a subfolder of the PoC
- check the 'demo' part of the code. what is it for? 
