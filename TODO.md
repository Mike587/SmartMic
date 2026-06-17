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

- [ ] **Centralize `config_path`.** `config_path = script_dir / "config.ini"` is
      duplicated verbatim in every module. Resolve it once (e.g. in
      `zeiss_paths`) and import it.

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

      NOTE: `set_logging` was vendored AS-IS (loguru) to keep log formatting
      identical and the change reviewable. Standardizing all logging onto the
      stdlib `MS_Helper_function.setup_run_logger` (and dropping loguru) is the
      separate "two logging systems coexist" item below — best folded into the
      shared gRPC channel context manager pass.

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

- [ ] **Imported experiment never deleted** in `check_experiment_api` — the clone
      is deleted, but the experiment imported from XML (~line 403) is left loaded
      in ZEN on every run.

- [ ] **Two default CZI-name schemes** — `check_experiment_api` builds
      `zenapi_myimage_<uuid>` inline while `_make_czi_basename` produces
      `zenapi_<uuid>`. Use the helper. Its return annotation also omits `None`
      (`snap_path` can be `None`).

- [ ] **Two logging systems coexist** — stdlib `setup_run_logger`
      (`MS_Helper_function`, used by the PoC) vs loguru `set_logging` (now
      vendored in `MS_zenapi_helpers`, used by every `MS_zenapi_*` module).
      Consolidate onto `setup_run_logger` and drop loguru (also noted under the
      vendoring item above).

- [x] **README projects table lists only `smartmic_poc`** — NOT a bug, this is
      by design. `smartmic_poc` is the only *public/shared* project; all other
      `projects/` folders (e.g. `HD_Nuclei_from_slide`, `Marc_SM`) are local-only
      and git-ignored on purpose. Documented this intent in the README's Projects
      section (2026-06-17) so the table isn't mistaken for stale.

- [ ] **PoC ignores `run_experiment`'s returned `exp_result_path`** and re-globs
      the folder for the newest `*.czi` (`MS_SmartMic_PoC.py` ~line 237) — fragile
      against leftover files; use the returned path.

- [ ] **PoC `itertools.groupby` assumes same-well positions are contiguous** after
      sorting by `scene_index` (~line 163). True for current plates; document the
      assumption or sort by well.
