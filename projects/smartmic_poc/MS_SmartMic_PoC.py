"""
MS_SmartMic_PoC.py

Proof-of-concept Smart Microscope automation pipeline.

High-level workflow
-------------------
For each well-plate position loaded from POSITIONS_FILE:

  1. OVERVIEW PASS
     a. Move XY stage to well position.
     b. Set objective + optovar (must happen before DF/SWAF so the optics
        are parfocal for the focus search).
     c. DefiniteFocus FindSurface  — locates the coverslip/sample surface.
        Subsequent DFs start 100 µm below the last known surface to avoid
        a full 3 mm sweep every time.
     d. SWAF coarse (DV_001_swaf_001) + SWAF fine (DV_001_swaf_002)
        — widefield software autofocus to fine-tune Z for the overview channel.
     e. Acquire widefield overview image (DAPI_GFP_001).
     f. Run cell-body / nuclei detection analysis on the overview CZI
        (external Python script in its own pixi environment).

  2. DETAILED PASS  (only if nuclei were detected in the overview)
     For each of up to 3 randomly selected nuclei:
     a. Move XY stage to the nucleus centroid (absolute coords from analysis).
     b. Set objective + optovar.
     c. DefiniteFocus FindSurface (adaptive start Z).
     d. SWAF coarse (DAPI_LSM_onez_001_swaf_001) + SWAF fine (_002)
        — LSM-channel autofocus, parfocal with the confocal acquisition.
     e. Acquire single-plane reference image (DAPI_LSM_onez_001) to verify
        focus at the SWAF2 position before committing to the z-stack.
     f. Acquire confocal z-stack (DAPI_LSM_z-stack_001, 11 planes × 2 µm).
     g. Log z-stack first/center/last Z and best-plane focus score.

Focus notes
-----------
- DF FindSurface returns the ZDrive (encoder) position.  The actual imaging
  plane (FocusPosition in the CZI XML) differs because DF applies a piezo /
  correction offset on top of the encoder value.
- DV SWAF and LSM SWAF are parfocally offset by ~3 µm on this system.  Always
  use the channel-matched SWAF experiment for each modality.
- last_surface_z_m tracks the surface Z across all positions so that each
  subsequent DF can start just 100 µm below, reducing sweep time from ~15 s
  to ~1 s.
"""

import json
import random
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration — edit these to adapt the pipeline to a different sample /
# experiment set.  All paths use forward slashes; Windows handles both.
# ---------------------------------------------------------------------------

# Root output directory.  Each run creates its own timestamped sub-folder beneath
# this path (see main()), so outputs from different runs never mix or overwrite.
ROOT_PATH = Path("F:/UserData/api")

# Leaf folder names created inside each per-run folder.
OVERVIEW_IMAGE_DIRNAME = "overview-images"     # widefield overview CZI files
ANALYSIS_DIRNAME       = "overview-analysis"   # targets.json outputs from analysis
DETAILED_DIRNAME       = "detailed-images"     # confocal single-plane + z-stack CZIs
LOG_DIRNAME            = "log"                  # timestamped run logs

# ZEN experiment files — run BY PATH from the copies vendored with this project
# (base_experiments/), not by name from ZEN's experiment library.  This makes the
# pipeline self-contained: the experiments travel with the repo and do not need to
# be pre-installed in ZEN.  PROJECT_DIR resolves relative to this script so the
# paths hold regardless of the working directory the pipeline is launched from.
PROJECT_DIR          = Path(__file__).resolve().parent
BASE_EXPERIMENTS_DIR = PROJECT_DIR / "base_experiments"

OVERVIEW_EXPERIMENT_PATH  = BASE_EXPERIMENTS_DIR / "DAPI_GFP_001.czexp"          # widefield overview (20×, DV)
DETAILED_EXPERIMENT_PATH  = BASE_EXPERIMENTS_DIR / "DAPI_LSM_z-stack_001.czexp"  # confocal z-stack (11 planes, 2 µm step)
ONCZ_EXPERIMENT_PATH      = BASE_EXPERIMENTS_DIR / "DAPI_LSM_onez_001.czexp"     # single confocal plane — focus reference
                                                                                 # acquired at SWAF2 Z before the z-stack

# Software autofocus experiments for the OVERVIEW (widefield/DV channel)
# These are parfocal with DAPI_GFP_001.
SWAF_EXPERIMENT_PATH      = BASE_EXPERIMENTS_DIR / "DV_001_swaf_001.czexp"   # coarse sweep — large Z range
SWAF_EXPERIMENT_PATH_2    = BASE_EXPERIMENTS_DIR / "DV_001_swaf_002.czexp"   # fine sweep — small Z range around SWAF1 result

# Software autofocus experiments for NUCLEUS DETAILED imaging (confocal/LSM channel)
# These are parfocal with DAPI_LSM_onez_001 and DAPI_LSM_z-stack_001.
# On this system the DV→LSM parfocality offset is ~3 µm, which is why separate
# SWAF experiments are needed for each modality.
NUCLEUS_SWAF_EXPERIMENT_PATH   = BASE_EXPERIMENTS_DIR / "DAPI_LSM_onez_001_swaf_001.czexp"   # coarse
NUCLEUS_SWAF_EXPERIMENT_PATH_2 = BASE_EXPERIMENTS_DIR / "DAPI_LSM_onez_001_swaf_002.czexp"   # fine

# Adaptive DF start Z: after the first successful FindSurface, all subsequent
# FindSurface calls start this far below the last known surface position.
# 100 µm is enough safety margin while reducing the sweep from ~3300 µm to ~100 µm.
DF_APPROACH_MARGIN_M = 100e-6   # metres

# Required sample carrier.  The pipeline's positions, objectives and focus
# settings are tuned for this specific plate, so the run aborts immediately
# if a different carrier is loaded in ZEN (queried via SampleCarrierService).
EXPECTED_SAMPLE_CARRIER = "Multichamber 384"

# Well-plate position file (.czexp) — contains the XYZ coordinates and
# IsUsedForAcquisition flags for each well / sub-position.  Read from the copy
# vendored with this project (position_files/) so the pipeline is self-contained.
POSITIONS_FILE = PROJECT_DIR / "position_files" / "384WP_TestPositions_004.czexp"

# External nuclei-detection script (runs in a separate pixi environment so it
# can have its own dependencies without conflicting with the ZEN API env).
ANALYSIS_SCRIPT_DIR = Path(r"C:\Users\zeiss\Mike\Image_Analysis\ia_PoC_002")
ANALYSIS_SCRIPT     = ANALYSIS_SCRIPT_DIR / "analyze_czi.py"

# ---------------------------------------------------------------------------

# This script lives in projects/smartmic_poc/.  Add the repo root (two levels
# up) to sys.path so the MS_* wrapper modules and zeiss_paths resolve, then
# import zeiss_paths which extends sys.path further for the Zeiss-provided
# zen_api / zen_api_utils packages (in the ZEN-API example folder).  This makes
# imports work regardless of the working directory the script is launched from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import zeiss_paths  # noqa: F401  — side effect: extends sys.path

try:
    import MS_CD7_API_LoA as ms          # synchronous wrappers around the ZEN gRPC API
    import MS_Helper_function as helper   # logging, position loading, focus scoring
    from MS_image_analysis import run_analysis  # external analysis-script launcher
except ImportError as e:
    print(f"[ERROR] Could not import MS_CD7_API_LoA: {e}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main():
    # Per-run output folder: F:/UserData/api/run_YYYYmmdd_HHMMSS/...
    # One timestamp groups this run's images, analysis and log together and keeps
    # separate runs from overwriting each other.
    run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_path      = ROOT_PATH / f"run_{run_timestamp}"
    overview_image_path = run_path / OVERVIEW_IMAGE_DIRNAME
    analysis_path       = run_path / ANALYSIS_DIRNAME
    detailed_folder     = run_path / DETAILED_DIRNAME
    log_folder          = run_path / LOG_DIRNAME

    log, _ = helper.setup_run_logger(log_folder)
    log.info("SmartMic PoC run started")
    log.info(f"Run output folder : {run_path}")
    log.info(f"Positions file    : {POSITIONS_FILE}")
    log.info(f"Overview exp      : {OVERVIEW_EXPERIMENT_PATH.name}")
    log.info(f"Detailed exp      : {DETAILED_EXPERIMENT_PATH.name}")
    log.info(f"Overview SWAF     : {SWAF_EXPERIMENT_PATH.name} / {SWAF_EXPERIMENT_PATH_2.name}")
    log.info(f"Nucleus SWAF      : {NUCLEUS_SWAF_EXPERIMENT_PATH.name} / {NUCLEUS_SWAF_EXPERIMENT_PATH_2.name}")

    # ------------------------------------------------------------------
    # Verify every experiment / position file this run needs is present
    # BEFORE touching hardware.  These are now loaded BY PATH from the
    # vendored copies, so a missing/renamed file would otherwise surface as
    # a FileNotFoundError mid-run — and the per-position acquisition calls
    # below are not all individually guarded.  Fail fast with a clear list.
    # ------------------------------------------------------------------
    required_files = {
        "Positions file"  : POSITIONS_FILE,
        "Overview exp"    : OVERVIEW_EXPERIMENT_PATH,
        "Detailed exp"    : DETAILED_EXPERIMENT_PATH,
        "Oncz exp"        : ONCZ_EXPERIMENT_PATH,
        "Overview SWAF 1" : SWAF_EXPERIMENT_PATH,
        "Overview SWAF 2" : SWAF_EXPERIMENT_PATH_2,
        "Nucleus SWAF 1"  : NUCLEUS_SWAF_EXPERIMENT_PATH,
        "Nucleus SWAF 2"  : NUCLEUS_SWAF_EXPERIMENT_PATH_2,
    }
    missing = [(label, path) for label, path in required_files.items() if not path.exists()]
    if missing:
        for label, path in missing:
            log.error(f"Required {label} not found: {path}")
        log.error(f"{len(missing)} required file(s) missing — aborting.")
        return 1
    log.info("All required experiment / position files present.")

    # ------------------------------------------------------------------
    # Standard start-of-run preflight BEFORE doing anything: gateway
    # responsive, microscope idle, correct carrier loaded, and stage
    # speed/acceleration set once for the run (full speed by default). The
    # whole pipeline assumes EXPECTED_SAMPLE_CARRIER; running it against a
    # different plate would drive the stage to wrong/unsafe positions.
    # ------------------------------------------------------------------
    if not ms.preflight(EXPECTED_SAMPLE_CARRIER, log):
        return 1

    # ------------------------------------------------------------------
    # Load positions
    # ------------------------------------------------------------------
    positions = helper.load_positions_from_czexp(POSITIONS_FILE)
    if not positions:
        log.error("No positions loaded — aborting.")
        return 1

    # Sort by scene_index so wells stay in plate order, then group by well.
    # Use an order-preserving dict rather than itertools.groupby: groupby only
    # groups CONSECUTIVE items, so if a well's positions are not contiguous in
    # scene order they would be split into several groups (and imaged as if they
    # were different wells). The dict groups all positions of a well regardless
    # of order, while wells are still visited in plate order (by the scene_index
    # of each well's first position).
    positions.sort(key=lambda p: p["scene_index"])
    wells_by_name = {}
    for p in positions:
        wells_by_name.setdefault(p["well"], []).append(p)
    wells = wells_by_name.items()
    log.info(f"Loaded {len(positions)} stage positions from {POSITIONS_FILE.name}")

    # Tracks the last successfully found surface Z (metres).
    # Used to compute an adaptive DF start position so subsequent FindSurface
    # calls avoid the full 3+ mm sweep from -300 µm.
    last_surface_z_m = None

    # ------------------------------------------------------------------
    # Outer loop: wells → positions
    # ------------------------------------------------------------------
    for well, well_positions in wells:
        log.info(f"=== Well {well} ===")

        for pos in well_positions:
            pos_name = pos["position_name"]
            tag      = f"{well}_{pos_name}"   # e.g. "D9_P1" — used in all filenames

            log.info(f"--- Position {tag}  x={pos['x_m']:.6f} m  y={pos['y_m']:.6f} m ---")

            # ── 1. Move XY stage ──────────────────────────────────────
            try:
                ms.move_stage_to_new_xy_position(pos["x_m"], pos["y_m"])
            except Exception as e:
                log.warning(f"Cannot reach position {tag}: {e} — skipping.")
                continue

            # ── 2. Set objective + optovar ────────────────────────────
            # Must happen BEFORE DF/SWAF so the focus search uses the same
            # optical path as the acquisition.
            # Objective 3 = Plan-Apochromat 20×/0.95
            # Optovar   1 = 2× tubelens
            ms.set_objective_set_optovar_sync(3, 1)

            # ── 3. DefiniteFocus FindSurface ──────────────────────────
            # First call starts at -300 µm (safe default).
            # All subsequent calls start DF_APPROACH_MARGIN_M below the last
            # known surface, reducing sweep time from ~15 s to ~1 s.
            df_start = (last_surface_z_m - DF_APPROACH_MARGIN_M
                        if last_surface_z_m is not None else None)
            _, _, df_attempts = ms.run_definite_focus_find_surface(start_z_m=df_start)
            z_fs_overview_um = ms.get_current_z_position() * 1e6
            last_surface_z_m = z_fs_overview_um * 1e-6
            log.info(f"FindSurface (overview) {tag}: zdrive={z_fs_overview_um:.3f} µm")
            if df_attempts > 1:
                log.warning(f"*** DF RETRY: FindSurface needed {df_attempts} attempts at {tag} ***")

            # ── 4. SWAF coarse + fine (widefield/DV channel) ──────────
            swaf_z_overview, swaf_attempts = ms.run_swaf_from_path(SWAF_EXPERIMENT_PATH)
            if swaf_z_overview is not None:
                log.info(f"SWAF1 (overview) {tag}: focus_pos={swaf_z_overview:.3f} µm"
                         f"  (DF-SWAF1: {z_fs_overview_um - swaf_z_overview:+.3f} µm)")
                if swaf_attempts > 1:
                    log.warning(f"*** SWAF1 RETRY: succeeded after {swaf_attempts} attempts at {tag} ***")
            else:
                log.warning(f"*** SWAF1 FAILED after {swaf_attempts} attempts (overview) {tag}"
                            f" — proceeding to SWAF2 from DF position ***")

            swaf_z_overview2, swaf2_attempts = ms.run_swaf_from_path(SWAF_EXPERIMENT_PATH_2)
            if swaf_z_overview2 is not None:
                ref = swaf_z_overview if swaf_z_overview is not None else z_fs_overview_um
                log.info(f"SWAF2 (overview) {tag}: focus_pos={swaf_z_overview2:.3f} µm"
                         f"  (SWAF1-SWAF2: {ref - swaf_z_overview2:+.3f} µm)")
                if swaf2_attempts > 1:
                    log.warning(f"*** SWAF2 RETRY: succeeded after {swaf2_attempts} attempts at {tag} ***")
            else:
                log.warning(f"*** SWAF2 FAILED after {swaf2_attempts} attempts (overview) {tag}"
                            f" — proceeding with SWAF1/DF position ***")

            # ── 5. Acquire overview image ─────────────────────────────
            # Use the result path returned by run_experiment instead of globbing
            # the folder for the newest *.czi: globbing is racy (can pick up a
            # leftover file) and misses the collision-counter suffix the
            # acquisition may append. exp_result_path is authoritative.
            ov_result = ms.run_experiment_from_path(OVERVIEW_EXPERIMENT_PATH, overview_image_path,
                                                    f"{tag}_overview")
            image_path = ov_result.get("exp_result_path")
            if image_path is None or not Path(image_path).exists():
                log.error(f"No overview CZI produced for {tag} — skipping.")
                continue
            image_path = Path(image_path)

            # Log the CZI's embedded FocusPosition and Laplacian-variance focus score.
            # FocusPosition ≠ ZDrive because DF applies a piezo correction on top.
            czi_z_overview       = helper.get_focus_position_from_czi(image_path)
            focus_score_overview = helper.compute_focus_score(image_path)
            score_str = f"{focus_score_overview:.1f}" if focus_score_overview is not None else "n/a"
            if czi_z_overview is not None:
                log.info(f"Overview acquired: {tag}_overview"
                         f"  czi_FocusPos={czi_z_overview:.3f} µm"
                         f"  (DF_offset={czi_z_overview - z_fs_overview_um:+.3f} µm vs zdrive)"
                         f"  focus_score={score_str}")
            else:
                log.info(f"Overview acquired: {tag}_overview  zdrive={z_fs_overview_um:.3f} µm"
                         f"  (czi_FocusPos not found)  focus_score={score_str}")

            # ── 6. Nuclei detection ───────────────────────────────────
            # Runs the analysis script as a subprocess; outputs a targets.json
            # with one entry per detected target (nucleus here), including
            # absolute XY coords.  `targets.json` is the general analysis
            # contract; the entries happen to be nuclei for this pipeline.
            success      = run_analysis(image_path, analysis_path, tag, log,
                                        ANALYSIS_SCRIPT, ANALYSIS_SCRIPT_DIR)
            targets_json = analysis_path / f"{tag}_targets.json"
            if not success or not targets_json.exists():
                log.warning(f"Analysis produced no targets.json for {tag} — skipping detailed imaging.")
                continue

            nuclei = json.loads(targets_json.read_text())
            if not nuclei:
                log.info(f"No nuclei detected at {tag} — moving to next position.")
                continue

            # ── 7. Select nuclei for detailed imaging ─────────────────
            # Pick up to 3 at random to keep run time predictable.
            targets = random.sample(nuclei, min(3, len(nuclei)))
            log.info(f"{len(nuclei)} nuclei detected — imaging {len(targets)} at random.")

            # ── 8. Inner loop: detailed imaging per nucleus ───────────
            for nucleus in targets:
                x, y   = nucleus["abs_x_m"], nucleus["abs_y_m"]
                nuc_id = nucleus["id"]
                log.info(f"Moving to nucleus {nuc_id}  x={x:.6f} m  y={y:.6f} m")
                try:
                    # 8a. Move to nucleus centroid
                    ms.move_stage_to_new_xy_position(x, y)

                    # 8b. Set objective + optovar (same as overview on this system)
                    ms.set_objective_set_optovar_sync(3, 1)

                    # 8c. DefiniteFocus FindSurface (adaptive start Z)
                    df_start_nuc = (last_surface_z_m - DF_APPROACH_MARGIN_M
                                    if last_surface_z_m is not None else None)
                    _, _, df_nuc_attempts = ms.run_definite_focus_find_surface(start_z_m=df_start_nuc)
                    z_fs_nuc_um = ms.get_current_z_position() * 1e6
                    last_surface_z_m = z_fs_nuc_um * 1e-6
                    log.info(f"FindSurface (nucleus {nuc_id:04d}) {tag}: zdrive={z_fs_nuc_um:.3f} µm")
                    if df_nuc_attempts > 1:
                        log.warning(
                            f"*** DF RETRY: FindSurface needed {df_nuc_attempts} attempts"
                            f" (nucleus {nuc_id:04d}) {tag} ***"
                        )

                    # 8d. SWAF coarse + fine (LSM channel — parfocal with the confocal acquisition)
                    # These experiments use the DAPI confocal channel, so SWAF2 sets Z
                    # exactly where the confocal will image.  Using DV SWAF here would
                    # introduce a ~3 µm parfocality error for the LSM acquisitions.
                    swaf_z_nuc, swaf_nuc_attempts = ms.run_swaf_from_path(NUCLEUS_SWAF_EXPERIMENT_PATH)
                    if swaf_z_nuc is not None:
                        log.info(f"SWAF1 (nucleus {nuc_id:04d}) {tag}: focus_pos={swaf_z_nuc:.3f} µm"
                                 f"  (DF-SWAF1: {z_fs_nuc_um - swaf_z_nuc:+.3f} µm)")
                        if swaf_nuc_attempts > 1:
                            log.warning(
                                f"*** SWAF1 RETRY: succeeded after {swaf_nuc_attempts} attempts"
                                f" (nucleus {nuc_id:04d}) {tag} ***"
                            )
                    else:
                        log.warning(
                            f"*** SWAF1 FAILED after {swaf_nuc_attempts} attempts"
                            f" (nucleus {nuc_id:04d}) {tag} — proceeding to SWAF2 from DF position ***"
                        )

                    swaf_z_nuc2, swaf_nuc2_attempts = ms.run_swaf_from_path(NUCLEUS_SWAF_EXPERIMENT_PATH_2)
                    if swaf_z_nuc2 is not None:
                        ref_nuc = swaf_z_nuc if swaf_z_nuc is not None else z_fs_nuc_um
                        log.info(f"SWAF2 (nucleus {nuc_id:04d}) {tag}: focus_pos={swaf_z_nuc2:.3f} µm"
                                 f"  (SWAF1-SWAF2: {ref_nuc - swaf_z_nuc2:+.3f} µm)")
                        if swaf_nuc2_attempts > 1:
                            log.warning(
                                f"*** SWAF2 RETRY: succeeded after {swaf_nuc2_attempts} attempts"
                                f" (nucleus {nuc_id:04d}) {tag} ***"
                            )
                    else:
                        log.warning(
                            f"*** SWAF2 FAILED after {swaf_nuc2_attempts} attempts"
                            f" (nucleus {nuc_id:04d}) {tag} — proceeding with SWAF1/DF position ***"
                        )

                    # 8e. Single-plane reference image at SWAF2 focus position.
                    # Acquired before the z-stack so we can verify focus quality
                    # without waiting for the full stack.  Non-fatal if the
                    # experiment is missing — the z-stack still runs.
                    try:
                        ms.run_experiment_from_path(
                            ONCZ_EXPERIMENT_PATH,
                            detailed_folder,
                            f"{tag}_nucleus_{nuc_id:04d}_oncz",
                        )
                    except Exception as oncz_err:
                        log.warning(f"oncz experiment skipped for nucleus {nuc_id:04d} at {tag}: {oncz_err}")

                    # 8f. Confocal z-stack
                    # The z-stack is centered on the current focus position (SWAF2).
                    # 11 planes × 2 µm = 20 µm total range, ±10 µm around SWAF2.
                    zstack_result = ms.run_experiment_from_path(
                        DETAILED_EXPERIMENT_PATH,
                        detailed_folder,
                        f"{tag}_nucleus_{nuc_id:04d}",
                    )

                    # 8g. Log z-stack metadata and focus quality.
                    # Use the returned result path (authoritative; accounts for any
                    # collision-counter suffix) rather than reconstructing the name.
                    nuc_czi_raw     = zstack_result.get("exp_result_path")
                    nuc_czi         = Path(nuc_czi_raw) if nuc_czi_raw else None
                    nuc_exists      = nuc_czi is not None and nuc_czi.exists()
                    czi_z_nuc       = helper.get_focus_position_from_czi(nuc_czi) if nuc_exists else None
                    focus_score_nuc = helper.compute_focus_score(nuc_czi)        if nuc_exists else None
                    nuc_score_str   = f"{focus_score_nuc:.1f}" if focus_score_nuc is not None else "n/a"
                    if czi_z_nuc is not None:
                        log.info(f"Detailed image saved: {tag}_nucleus_{nuc_id:04d}.czi"
                                 f"  czi_FocusPos={czi_z_nuc:.3f} µm"
                                 f"  (DF_offset={czi_z_nuc - z_fs_nuc_um:+.3f} µm vs zdrive)"
                                 f"  focus_score={nuc_score_str}")
                    else:
                        log.info(f"Detailed image saved: {tag}_nucleus_{nuc_id:04d}.czi"
                                 f"  zdrive={z_fs_nuc_um:.3f} µm  (czi_FocusPos not found)"
                                 f"  focus_score={nuc_score_str}")

                    # Log first/center/last plane Z so we can verify the stack
                    # is centered on the SWAF2 focus position.
                    zr = helper.get_zstack_z_range(nuc_czi) if nuc_exists else None
                    if zr:
                        log.info(
                            f"Z-stack range: first={zr['first_um']:.3f} µm  "
                            f"center={zr['center_um']:.3f} µm  "
                            f"last={zr['last_um']:.3f} µm  "
                            f"({zr['n_z']} planes @ {zr['step_um']:.1f} µm)"
                        )

                except Exception as e:
                    log.warning(f"Skipping nucleus {nuc_id} at {tag}: {e}")

    log.info("All positions processed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
