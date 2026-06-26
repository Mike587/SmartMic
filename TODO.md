
# Notes by Mike

Some notes before I forget:

- Incubation (temperature and CO2 and Nitrogen content): Can we do this over the API?
- Stage speed: Is there a good way to control stage speed? is this in the czexp files? How to globally control stage speed for a whole project?

## Plan: Stage speed (investigated 2026-06-26)

**Answer to the questions:** Yes, the API supports it; no, it is not in the
`.czexp` files; global control is done via a module-level constant in
`MS_zenapi_stage_LM.py`.

`MS_zenapi_stage_LM.py` already imports from `zen_api.lm.hardware.v2`, which
exposes speed/acceleration control as separate calls (the `MoveTo` request
itself has no velocity field — speed is set on the stage before the move):

- `StageServiceSetSpeedRequest(speed_x, speed_y)` — both `Optional[float]`, % `[0, 100]`
- `StageServiceSetAccelerationRequest(acceleration_x, acceleration_y)` — same units
- `StageServiceGetSpeedRequest` / `StageServiceGetAccelerationRequest` (+ responses) for read-back

Speed is **not** stored in `.czexp` — it is a hardware-layer setting, so it
cannot be controlled per-experiment from the XML. The global control is a
one-shot call at the start of a project run.

**Default is full speed — the knob is for sensitive samples only.**
Normal projects run at 100% speed and 100% acceleration (the fast, default
behaviour). The control exists for the exception: some samples need it turned
down. When turning it down, note that for live samples (cells, spheroids,
suspended media) it is the *acceleration* (the jerk at the start/stop of a move)
that sloshes the medium and disturbs the sample, not the top speed — so
acceleration is the knob that matters most. Always set both together: a low top
speed paired with a high acceleration still jolts the sample. `v2` exposes both:
`StageServiceSetAccelerationRequest(acceleration_x, acceleration_y)` alongside
`StageServiceSetSpeedRequest(speed_x, speed_y)`, both % `[0, 100]`.

**Persistence (decided 2026-06-26): set once per project, leave it.**
`SetSpeed` / `SetAcceleration` are separate calls (not fields on `MoveTo`) with
matching `GetSpeed` / `GetAcceleration` read-backs — i.e. they are *device
state*. Once set, the stage controller holds the values and every later `MoveTo`
uses them, including across SmartMic's per-call open/close of the gRPC channel.
So the intended usage is to set speed **and acceleration** once at the start of
a project run and not restore them — the earlier get→set→move→restore idea is
explicitly dropped, since restoring would defeat the goal.

Caveat to verify empirically (not guaranteed by the API): whether the value
survives a ZEN restart / hardware re-init, or whether ZEN reloads a profile
default. If it does NOT persist across restarts, "once per project" becomes
"once per run" — same code, just called at the top of every `main()` either way.

Proposed implementation (~30 lines, no new dependencies):

- [ ] Add two module-level default constants to `MS_zenapi_stage_LM.py`:
      `STAGE_ACCELERATION_PERCENT: float = 100.0` and
      `STAGE_TRAVEL_SPEED_PERCENT: float = 100.0` (full speed = normal-project
      default). Single source of truth; the "global control for a whole project"
      Mike asked for — two numbers to turn down for a sensitive sample.
- [ ] Add a standalone
      `async def set_stage_motion(speed_percent=None, acceleration_percent=None)`
      that sets **both together** in one call:
      `set_acceleration(StageServiceSetAccelerationRequest(acceleration_x=a, acceleration_y=a))`
      and `set_speed(StageServiceSetSpeedRequest(speed_x=s, speed_y=s))`.
      `None` falls back to the respective constant. **No restore.** Setting both
      together avoids the trap of a low top speed paired with a high (default)
      acceleration, which would still jolt the sample.
- [ ] Do NOT touch speed/acceleration inside `move_stage_to_new_xy_position` —
      moves just inherit whatever the controller currently holds.
- [ ] Call `set_stage_motion()` once near the top of the PoC `main()` (after
      channel/hardware is reachable, before the first acquisition), and log both
      `GetSpeed` and `GetAcceleration` at run start so each run's log records the
      values actually in effect (insurance against silent overwrites by ZEN / a
      tiles scan).
- [ ] Expose a thin `set_stage_motion_sync()` in `MS_CD7_API_LoA.py` to match
      the existing sync-wrapper pattern.
- [ ] Add an offline unit test (mock the stub) asserting both `set_acceleration`
      and `set_speed` are called once with the expected percents and that moves
      do not re-set them.
- [ ] One-time empirical check: set speed + acceleration → restart ZEN →
      `GetSpeed` / `GetAcceleration` to confirm whether the controller retains
      them (decides once-per-project vs once-per-run).

## Plan: Incubation — temperature / CO2 / N2 (investigated 2026-06-26)

**DECISION (2026-06-26): do nothing for now.** Incubation is set manually on the
incubator and that is acceptable. No control and no monitoring will be built at
this time. The investigation below is kept for reference if this is revisited
later — none of the items are scheduled.

**Answer:** Not over the ZEN gRPC API. A search across all 31 service stubs in
the `zen_api` package found no incubation, temperature, CO2, environment, or
climate service. `SampleCarrierService` only exposes carrier geometry. So this
cannot be done with the same gRPC layer the rest of SmartMic uses.

**`.czmac` macros over the API are also not possible (checked 2026-06-26).**
The gRPC API exposes no way to run a ZEN macro / OAD IronPython script. There is
no `ExecuteMacro` / `RunScript` / `Eval` method anywhere in `zen_api`, and the
ZEN-API README states it explicitly: *"ZEN API is not a replacement for
ZEN-internal Scripting based on IronPython (OAD)."* gRPC is "control from the
outside"; OAD/macros are "control from the inside" and the two don't bridge
directly. Near-misses that do NOT help: `WorkflowService` only runs pre-defined
ZEN job templates by name (no macro path / script body), and
`ExternalProcedureService` is a reverse callback channel (and EM-side). So the
"run an incubation macro over the API" shortcut is off the table.

**Priority (decided 2026-06-26): monitoring, not control.** Closed-loop control
from SmartMic is NOT required — it is fine to set temperature/CO2/N2 manually on
the incubator itself. What would be genuinely useful is to **read and log** the
current values during a run (and optionally warn/abort if they drift out of
tolerance). So aim for read-only first; control is a non-goal for now.

- [ ] **Identify the incubator make/model on this system** (likely Okolab,
      Ibidi, or Pecon) — this decides whether monitoring is even reachable from
      Python. Do this first.

- [ ] **Primary: read-only monitoring via vendor SDK.** Most of these vendors
      ship a Python/serial API (e.g. Okolab H401-T-CONTROLLER has a documented
      Python SDK). Add a thin `MS_incubation.py` that just *reads* current
      temperature / CO2 / N2, logs them at run start (and optionally
      periodically), and raises if outside a configured tolerance. No control, no
      coupling to ZEN. This is the wanted outcome if the SDK exposes reads.

- [ ] **Fallback if no SDK / reads aren't reachable:** check whether the
      incubator controller has a serial/USB or HTTP status endpoint we can poll
      directly for read-only values, independent of ZEN.

- [ ] **Not preferred — ZEN COM / OAD bridge (control or monitoring).** ZEN
      Blue's OAD layer (`Zen.Devices.Incubator.*`) can reach the incubator if it
      is registered as a ZEN device, driven from Python via `win32com`
      (`ZeissOAD.Application`), or via a file-drop/watcher bridge where a
      long-running OAD macro inside ZEN polls a folder. Both work but couple
      SmartMic to ZEN being open in the foreground and add a moving part — only
      revisit if there is no vendor-SDK / direct-controller read path at all.

- [ ] **Stretch (non-goal for now): set-point control** via the vendor SDK, only
      if monitoring is in place and there is a clear need.






# SmartMic — Code Review TODO

Proposed changes from a read-only review (2026-06-16). Grouped by severity.
None of these have been applied yet.

## Bugs / broken code

- [x] **Fix broken `__main__` in `MS_zenapi_stage_LM.py`** (line ~179). ✅ DONE
      `asyncio.run(main(sys.argv))` references a `main` that does not exist in
      this module → `NameError` when run directly. Either add a `main`
      (as in `objectivechanger`/`swaf`) or remove the guard and the now-unused
      `import sys`.

- [x] **Fix return-type mismatch in `setup_run_logger`** (`MS_Helper_function.py:27`). ✅ DONE
      Annotated `-> logging.Logger` and docstring says it returns a logger, but
      it actually returns `(logger, log_file)`. Correct the annotation + docstring
      (or change the return) and audit callers.

- [x] **Wrong request type in `MS_zenapi_swaf.py:231`**. ✅ DONE
      `focus_service.get_position(StageServiceGetPositionRequest())` passes a
      Stage request to the Focus service. Use `FocusServiceGetPositionRequest`
      (and import it). Works today only because both messages are empty.

- [x] **Fix CWD-dependent config-path resolution in `MS_zenapi_experiment_methods.py`**. ✅ DONE
      Public funcs default to `configfile="config.ini"` (bare, CWD-relative) and
      the LoA wrappers never pass a path, so config resolves relative to the
      current working directory — contradicting the README and the in-file
      comment. Default to `script_dir / "config.ini"` like every other module,
      and/or have the LoA wrappers pass `config_path` explicitly.

## Duplication

- [x] **Single source of truth for the default output path** `F:/UserData/mike/api`. ✅ DONE
      Currently hardcoded in ~5 places: `DEFAULT_EXPERIMENT_OUTPUT_FOLDER` in both
      `MS_Helper_function.py` and `MS_CD7_API_LoA.py`, plus literal fallbacks in
      `check_experiment_api`, `run_experiment_from_xml`, `run_experiment_from_path`,
      and the module-level `image_folder`. Define once and import.

- [x] **Factor out the shared acquisition boilerplate in `MS_zenapi_experiment_methods.py`**. ✅ DONE
      `check_experiment_api`, `run_experiment_from_xml`, and `run_experiment_from_path`
      are ~90% identical (channel setup → output-folder resolution → filename
      collision counter → move result out of default folder → temp cleanup).
      Extract a shared `_acquire_and_collect(exp_id, ...)` helper.
      Also unify the two forms of the collision counter (`while True`/break vs
      `while (...).exists()`).

- [x] **Make experiment-run return shapes uniform**. ✅ DONE
      `check_experiment_api` now also returns `experiment_id` (docstring updated),
      matching `run_experiment_from_path/xml`, so the LoA `run_experiment` result
      carries it too.

## Dead code / unused imports

- [x] **`MS_zenapi_focus.py`** — removed unused `definite_focus_service` construction
      in `move_focus_to_new_z_position` and `get_current_z_focus_position`, and
      removed unused `import sys`. ✅ DONE
      (Note: `definite_focus_recall` *does* use the stub for `recall_focus`, so
      it was left intact — the original TODO listing it was wrong.)

- [x] **`MS_zenapi_experiment_methods.py`** — removed the no-op `global image_folder`,
      and moved the display-only imports (`matplotlib` **and** `pylibCZIrw`, both
      used only in the `__main__` CZI-display block) into that block. ✅ DONE

- [x] **`MS_zenapi_stage_LM.py`** — removed the unused `zen_api_utils.stage` import
      (`get_stageXY_position_simple`, `move_to_stageXY_position_simple`,
      `StageXYPosition`) and the redundant `new_posx = x` / `new_posy = y`
      assignments. Also removed the dead commented-out reference block that was
      the imports' only user, since keeping it would reference removed names. ✅ DONE

## Comment / labeling fixes

- [x] **"Exponential backoff" is actually linear** in `MS_zenapi_focus.py`
      (docstring line ~73, comment line ~159). Relabeled both as "linear" to
      match the actual `2.0 * (attempt + 1)` → 2, 4, 6 s and the already-correct
      `definite_focus_recall`. ✅ DONE

- [x] **Stale reference** in `MS_Helper_function.py:87` docstring —
      replaced "Uses the same XML parser as extract_positions.py" with a
      description of the stdlib `xml.etree` parser actually used. ✅ DONE

- [x] **Document the `_bool` default-to-True behavior** (`MS_Helper_function.py:112`):
      added a comment explaining a missing `IsUsedForAcquisition` tag is treated
      as *used* (matching ZEN, which only writes the flag when False). ✅ DONE

## Minor

- [x] File headers carried the original Zeiss filename as a second `File :` line
      in the first header block, clashing with the `MS_*` `File :` line below it.
      Relabeled the upstream line to `Based on :` (preserving the Zeiss
      attribution/copyright) across all five `MS_zenapi_*` modules
      (`experiment_methods`, `focus`, `stage_LM`, `objectivechanger`, `swaf`),
      not just the two originally flagged. ✅ DONE

- [x] `MS_zenapi_swaf.py` `main()` relied on a module-global `logger` only
      assigned in the `__main__` guard — would `NameError` if imported and
      called directly. Now creates its own `logger = set_logging()` so it is
      self-contained. ✅ DONE

## Structural / cross-module (deferred — touch every module, do as one pass)

These were noted during the review but not part of the original list. They are
larger refactors that touch all `MS_zenapi_*` modules at once, so they should be
done deliberately as a single pass rather than folded into small edits.

- [x] **Shared gRPC channel context manager.** ✅ DONE (2026-06-17).
      `MS_zenapi_helpers.open_zen_channel(config_file)` is an
      `@asynccontextmanager` that wraps `initialize_zenapi` and closes the
      channel in its `finally`. Every async function across `focus`, `stage_LM`,
      `objectivechanger`, `swaf`, `sample_carrier`, and `experiment_methods` now
      uses `async with open_zen_channel(config_path) as (channel, metadata): ...`
      — `initialize_zenapi(` and `channel.close()` no longer appear in any
      wrapper module (only inside the helper). Verified: imports clean, and a
      unit test confirms the channel closes on both the normal-exit and
      exception paths.

- [x] **Fix inconsistent channel cleanup (folds into the item above).** ✅ DONE.
      The functions that previously called `channel.close()` only on the happy
      path (`focus`, `stage_LM`, `experiment_methods`) — and so leaked the
      channel when a gRPC call raised — now close uniformly via the context
      manager's `finally`. The `try/finally` versions (`sample_carrier`, the
      newer `objectivechanger` ones) were converted to the same helper.

- [x] **Centralize `config_path`.** ✅ DONE (2026-06-17). Resolved once as
      `zeiss_paths.CONFIG_PATH` (absolute, repo root); all six wrapper modules now
      do `from zeiss_paths import CONFIG_PATH as config_path` instead of
      recomputing `Path(__file__).parent / "config.ini"`. The now-unused
      `from pathlib import Path` was dropped from the four modules that only used
      it for that. `MS_zenapi_helpers` stays decoupled from `zeiss_paths` (its
      `initialize_zenapi`/`open_zen_channel` keep their plain `"config.ini"`
      default; the modules pass `config_path` explicitly).

- [x] **Reduce / vendor the `zen_api_utils` dependency.** ✅ DONE (2026-06-17).
      All three usage surfaces are now vendored into `MS_zenapi_helpers.py`, so
      the SmartMic library imports only `zen_api` and no longer needs the
      un-packaged `zen_api_utils` example glue on `sys.path`:

      * `zen_api_utils.objective` — the 4 position-lookup helpers
        (`get_used_objective_positions`, `get_used_optovar_positions`,
        `get_objective_by_position`, `get_optovar_by_position`) are vendored
        (duck-typed, no `zen_api` type imports) and imported by
        `MS_zenapi_objectivechanger`.
      * `zen_api_utils.experiment` (DEMO-ONLY) — `show_swaf_info_LM` is inlined
        as `MS_zenapi_swaf._show_swaf_info`; `save_experiment` is replaced by a
        direct `exp_service.save(...)` call in `MS_zenapi_swaf.main()`.
      * `zen_api_utils.misc` — `initialize_zenapi` and `set_logging` are vendored
        verbatim into `MS_zenapi_helpers` and imported by all six wrapper
        modules. `verify_zen_api.py`, the README and `zeiss_paths.py` were
        updated to drop the `zen_api_utils` references.

      NOTE: `set_logging` was initially vendored AS-IS (loguru) to keep the
      change reviewable, then later reimplemented on the stdlib `logging` stack —
      see the now-resolved "two logging systems coexist" item below. The library
      no longer imports loguru.

---

**Suggested first targets:** the config-path inconsistency (latent failure)
and the `setup_run_logger` return-type mismatch.

---

## New review (2026-06-17)

A second read-only pass. Items marked DONE were fixed in this pass; the rest are
newly logged and not yet applied.

### Bugs / data-loss

- [x] **`_move_result_and_cleanup` could delete the acquired result and unrelated
      CZIs** (`MS_zenapi_experiment_methods.py`). The cleanup globbed `*.czi` in
      ZEN's default folder and unlinked every match. When `custom_image_folder`
      resolves to the same folder ZEN saves to, the result was not moved and was
      then deleted by the cleanup loop → lost acquisition. It also deleted
      unrelated CZIs (other experiments / a concurrent run's in-flight file),
      contradicting the "unique across concurrent runs" comment. ✅ DONE — the
      move check now compares resolved paths, and the result file is excluded
      from the cleanup deletion set.

- [x] **Blocking `time.sleep()` inside `async def`** in
      `MS_zenapi_stage_LM.move_stage_to_new_xy_position` (~line 136) and
      `MS_zenapi_experiment_methods.check_experiment_api` (~line 468) — blocks the
      event loop (relevant given `qasync` is a dependency). ✅ DONE — replaced
      with `await asyncio.sleep(...)`; removed the now-unused `import time` in
      both modules.

### Dead code

- [x] **`MS_zenapi_objectivechanger.set_objective_set_optovar`** — removed the
      unused `obj_initial_position` / `optovar_initial_position` captures and the
      unused `out =` binding on the two `move_to` calls. ✅ DONE

- [x] **`MS_CD7_API_LoA.set_objective_set_optovar_sync`** — hoisted the
      function-local `import time` to a module-level import. ✅ DONE

### Open (not yet fixed)

- [x] **Channel leaks on error paths.** ✅ DONE (2026-06-17) — fixed by the
      `open_zen_channel()` context-manager pass (see the Structural section
      above). `move_focus_to_new_z_position`, `check_experiment_api` and the
      `run_experiment_*` functions now close the channel on every path.

- [x] **`check_experiment_api` conflated smoke test with production** — ✅ DONE
      (2026-06-17). Split out a lean `run_experiment_by_name` (load → run →
      collect, no round-trip, no snap/live) and repointed `MS_CD7_API_LoA.run_experiment`
      to it; `do_snap_and_live=True` still routes to `check_experiment_api` for
      the full smoke test. The PoC's per-image acquisitions therefore no longer
      run the clone/export/import/delete round-trip, so they no longer leave an
      imported experiment loaded in ZEN on every image.
      NOTE: `check_experiment_api` itself still imports one experiment per call
      that cannot be cleaned up — the ZEN `ExperimentService` exposes no
      delete-by-id or unload (`delete` takes only `experiment_name`, and an
      imported experiment has none). This is acceptable now that the function is
      an occasional smoke test rather than a per-image call; revisit if/when ZEN
      adds an unload API.

- [x] **Two default CZI-name schemes** — ✅ DONE (2026-06-17).
      `check_experiment_api` now builds its base name via `_make_czi_basename`
      (default `zenapi_<uuid>`, matching `run_experiment_from_xml` /
      `run_experiment_from_path`) and derives the snap name as `<base>_snap`.
      Its return annotation was widened to `Dict[str, Union[str, Path, None]]`
      (`snap_path` can be `None`).

- [x] **Two logging systems coexist** — ✅ DONE (2026-06-17). `set_logging` in
      `MS_zenapi_helpers` is now a stdlib `logging` helper (loguru dropped from
      the library) that returns the shared `"smartmic"` logger — the same name
      `MS_Helper_function.setup_run_logger` configures. So when the PoC calls
      `setup_run_logger` first, the wrapper modules' `set_logging()` reuses that
      configured logger and their output lands in the per-run log file; used
      standalone, `set_logging` attaches a UTF-8 stdout handler (idempotent, no
      duplicate handlers). Both stdout handlers now use `sys.stdout.reconfigure`
      instead of an owning `TextIOWrapper`, so they coexist without closing the
      shared buffer. (loguru remains in `pixi.toml` only as a transitive dep of
      other packages — the SmartMic library no longer imports it.)

- [x] **README projects table lists only `smartmic_poc`** — NOT a bug, this is
      by design. `smartmic_poc` is the only *public/shared* project; all other
      `projects/` folders (e.g. `HD_Nuclei_from_slide`, `Marc_SM`) are local-only
      and git-ignored on purpose. Documented this intent in the README's Projects
      section (2026-06-17) so the table isn't mistaken for stale.

- [x] **PoC ignores `run_experiment`'s returned `exp_result_path`** — ✅ DONE
      (2026-06-17). The overview pass now uses `ov_result["exp_result_path"]`
      instead of globbing for the newest `*.czi`, and the detailed z-stack pass
      uses `zstack_result["exp_result_path"]` instead of reconstructing the
      filename — both authoritative and collision-counter-safe.

- [x] **PoC `groupby` contiguity assumption** — ✅ DONE (2026-06-17). Replaced
      `itertools.groupby` (which only groups CONSECUTIVE items) with an
      order-preserving dict, so a well whose positions are non-contiguous in
      scene order is no longer split into multiple groups. Wells are still
      visited in plate order (by each well's first scene_index). Dropped the
      now-unused `import itertools`.

---

## New review (2026-06-23)

Pass over the run-by-path migration (`29c99e8`) + the `*_from_path` /
test-suite work (`144e459`).

- [x] **PoC migrated from run-by-name to run-by-path.** ✅ DONE (`29c99e8`).
      Closes the old HANDOVER follow-up: the PoC now loads SWAF and acquisition
      experiments via `run_swaf_from_path` / `run_experiment_from_path` from the
      vendored `base_experiments/` + `position_files/` copies, so it no longer
      depends on the experiments being pre-installed in ZEN's library.

- [x] **Per-run timestamped output folders.** ✅ DONE (`29c99e8`). Outputs go to
      `F:/UserData/api/run_<timestamp>/...`. Timestamp is 1-second resolution;
      same-second collision left as-is by design (documented in HANDOVER).

- [x] **Validate experiment / position files up front.** ✅ DONE (2026-06-23).
      `main()` checks all required `.czexp` + the positions file exist before
      touching hardware and aborts with a listed error if any are missing, rather
      than dying on a mid-run `FileNotFoundError` from an unguarded acquisition
      call.

- [ ] **Cleanup deletes everything in ZEN's default folder** — WON'T FIX
      (documented in HANDOVER). The default folder is ZEN-owned and the ZEN-API
      has no setter, so the pipeline cannot redirect it from code. Mitigation is
      operational: point ZEN's default save location at a scratch-only folder.

- [ ] **Stale docstring on `MS_CD7_API_LoA.run_experiment_from_path`** — says it
      "imports its XML via ExperimentService.Import" but the implementation
      *loads by path* (Import yields a non-runnable experiment). Fix the wording
      to match `run_experiment_from_path` in `MS_zenapi_experiment_methods`.

- [x] **`main()` demo blocks in `MS_zenapi_*` (Zeiss leftovers)** — ✅ DONE
      (2026-06-23). Removed the `main()` / `__main__` demo blocks from all six
      `MS_zenapi_*` modules (and the swaf `_show_swaf_info` demo helper + demo-only
      globals), so the modules are now library-only. Pruned the imports that became
      unused (`sys`, `asyncio` where only `asyncio.run` in the demo used it, the
      clone/save/SWAF-param/Focus stubs in swaf, the matplotlib/pylibCZIrw display
      block in experiment_methods). 92 offline unit tests still pass. The external
      `zen_api` gRPC package stays a dependency (see `zeiss_paths.py`); manual
      smoke-checking lives in the pytest suite + `verify_zen_api.py`.
