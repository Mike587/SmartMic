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

- [ ] **Shared gRPC channel context manager.** Every async function repeats the
      same boilerplate: `set_logging()` → `initialize_zenapi(config_path)` →
      build stub(s) → … → `channel.close()`. Extract an
      `async with open_zen_channel() as (channel, metadata): ...` helper and use
      it across `focus`, `stage_LM`, `objectivechanger`, `swaf`,
      `sample_carrier`, and `experiment_methods`.

- [ ] **Fix inconsistent channel cleanup (folds into the item above).** Some
      functions (`sample_carrier`, the newer `objectivechanger` ones) use
      `try/finally` so the channel closes on error; others (`focus`, `stage_LM`,
      `experiment_methods`) call `channel.close()` only on the happy path and
      leak the channel if a gRPC call raises. The context manager fixes this
      uniformly (close in its `__aexit__`).

- [ ] **Centralize `config_path`.** `config_path = script_dir / "config.ini"` is
      duplicated verbatim in every module. Resolve it once (e.g. in
      `zeiss_paths`) and import it.

- [ ] **Reduce / vendor the `zen_api_utils` dependency.** The code still leans on
      Zeiss example glue (`zen_api_utils`) rather than the raw `zen_api` stubs.
      `zen_api` is pip-installable; `zen_api_utils` is NOT packaged, so vendoring
      the few functions used is what would let SmartMic stop depending on the
      `python_examples` folder. Three usage surfaces, by effort:

      * `zen_api_utils.objective` (LOCAL, easy) — 4 trivial pure-Python helpers
        (`get_used_objective_positions`, `get_used_optovar_positions`,
        `get_objective_by_position`, `get_optovar_by_position`), used only in
        `MS_zenapi_objectivechanger.set_objective_set_optovar` and
        `get_current_objective_and_optovar_names`. ~15 lines to inline, no deps.
      * `zen_api_utils.experiment` (DEMO-ONLY, easy) — `show_swaf_info_LM` and
        `save_experiment`, used only in `MS_zenapi_swaf.main()` (the CLI demo;
        the production `run_software_autofocus` does not touch them).
      * `zen_api_utils.misc` (PERVASIVE, the real one) — `initialize_zenapi`
        (config.ini → SSL context → grpclib Channel + control-token metadata)
        and `set_logging` (a loguru logger). Called in essentially EVERY public
        function across all six modules. `initialize_zenapi` is ~30 lines of
        stdlib `configparser`/`ssl`/`grpclib` and SmartMic's config.ini already
        matches its `[api]` keys, so it vendors cleanly. For `set_logging`,
        prefer standardizing on the existing stdlib
        `MS_Helper_function.setup_run_logger` and dropping the loguru-based
        helper (broader logging-consistency change). Best done together with the
        "shared gRPC channel context manager" item above, since both wrap the
        connection setup.

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

- [ ] **Channel leaks on error paths.** `MS_zenapi_focus.move_focus_to_new_z_position`
      re-raises without `channel.close()` (~lines 316–322); `check_experiment_api`
      and the `run_experiment_*` helpers close only on the happy path. Folds into
      the deferred `open_zen_channel()` context-manager item above — the
      highest-value remaining reliability fix.

- [ ] **Imported experiment never deleted** in `check_experiment_api` — the clone
      is deleted, but the experiment imported from XML (~line 403) is left loaded
      in ZEN on every run.

- [ ] **Two default CZI-name schemes** — `check_experiment_api` builds
      `zenapi_myimage_<uuid>` inline while `_make_czi_basename` produces
      `zenapi_<uuid>`. Use the helper. Its return annotation also omits `None`
      (`snap_path` can be `None`).

- [ ] **Two logging systems coexist** — stdlib `setup_run_logger`
      (`MS_Helper_function`, used by the PoC) vs loguru `set_logging`
      (`zen_api_utils`, used by every `MS_zenapi_*` module). Consolidate onto
      `setup_run_logger` (also tracked under the `zen_api_utils` item above).

- [ ] **Stale README projects table** — lists only `projects/smartmic_poc`, but
      `projects/` also holds `HD_Nuclei_from_slide` and `Marc_SM`.

- [ ] **PoC ignores `run_experiment`'s returned `exp_result_path`** and re-globs
      the folder for the newest `*.czi` (`MS_SmartMic_PoC.py` ~line 237) — fragile
      against leftover files; use the returned path.

- [ ] **PoC `itertools.groupby` assumes same-well positions are contiguous** after
      sorting by `scene_index` (~line 163). True for current plates; document the
      assumption or sort by well.
