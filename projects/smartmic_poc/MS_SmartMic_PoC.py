"""
MS_SmartMic_PoC.py

Proof-of-concept Smart Microscope automation pipeline.

OUTDATED (2026-11-10): this pipeline is not maintained at the moment. It does
not use the current library practice (for example ``set_optics_verified``,
``run_analysis_json``, ``write_run_info``). Do not use it as a model for a new
project, and examine it before you run it on the scope. An update is planned.

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
- One DefiniteFocusTracker spans the whole run: it remembers the surface Z so
  each subsequent DF starts just 100 µm below, reducing sweep time from ~15 s
  to ~1 s, and stops the run if DF fails at too many positions in a row.
"""

import json
import os
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

# Optics used for every acquisition and focus search.
OBJECTIVE_NR = 3   # Plan-Apochromat 20×/0.95
OPTOVAR_NR   = 1   # 2× tubelens

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

# Locate SmartMic root: override with the SMARTMIC_ROOT env var if this
# project is ever moved or vendored elsewhere; defaults to the standard
# checkout location (same override pattern zeiss_paths.py uses for zen_api).
_SMARTMIC_ROOT = os.environ.get("SMARTMIC_ROOT", r"C:\Users\zeiss\Mike\SmartMic")
sys.path.insert(0, _SMARTMIC_ROOT)
import zeiss_paths  # noqa: F401  — side effect: extends sys.path

try:
    import MS_CD7_API_LoA as ms          # synchronous wrappers around the ZEN gRPC API
    import MS_Helper_function as helper   # logging, position loading, focus scoring
    from MS_image_analysis import run_analysis  # external analysis-script launcher
except ImportError as e:
    print(f"[ERROR] Could not import MS_CD7_API_LoA: {e}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Shared steps
# ---------------------------------------------------------------------------

def focus_at_current_xy(df, swaf_paths, what, tag, log):
    """Set the optics, find the surface with DF, then refine Z with SWAF coarse + fine.

    Args:
        df:         The run's DefiniteFocusTracker (adaptive start, fault stop).
        swaf_paths: (coarse, fine) SWAF experiments, parfocal with the acquisition.
        what:       "overview" or "nucleus 0007" — for the log lines.
        tag:        Position tag, e.g. "D9_P1".

    Returns:
        The DF surface Z in µm (ZDrive read-back), or None if DF failed (the
        caller skips the position; ``df.last_message`` says why). A failed SWAF
        is only logged — the acquisition proceeds from the previous Z.
    """
    # Optics BEFORE DF/SWAF so the focus search uses the acquisition's optical path.
    ms.set_objective_set_optovar_sync(OBJECTIVE_NR, OPTOVAR_NR)

    z_m = df.find_surface()
    if z_m is None:
        return None
    z_fs_um = z_m * 1e6
    log.info(f"FindSurface ({what}) {tag}: zdrive={z_fs_um:.3f} µm")
    if df.last_attempts > 1:
        log.warning(f"*** DF RETRY: FindSurface needed {df.last_attempts} attempts ({what}) {tag} ***")

    ref_um = z_fs_um   # SWAF1 is compared to DF, SWAF2 to SWAF1 (or DF if SWAF1 failed)
    stages = ((1, swaf_paths[0], "DF-SWAF1",    "proceeding to SWAF2 from DF position"),
              (2, swaf_paths[1], "SWAF1-SWAF2", "proceeding with SWAF1/DF position"))
    for n, path, delta, on_failure in stages:
        z_um, attempts = ms.run_swaf_from_path(path)
        if z_um is None:
            log.warning(f"*** SWAF{n} FAILED after {attempts} attempts ({what}) {tag} — {on_failure} ***")
            continue
        log.info(f"SWAF{n} ({what}) {tag}: focus_pos={z_um:.3f} µm  ({delta}: {ref_um - z_um:+.3f} µm)")
        if attempts > 1:
            log.warning(f"*** SWAF{n} RETRY: succeeded after {attempts} attempts ({what}) {tag} ***")
        ref_um = z_um
    return z_fs_um


def log_czi_focus(log, label, czi_path, z_fs_um):
    """Log the CZI's embedded FocusPosition and its Laplacian-variance focus score.

    FocusPosition ≠ ZDrive because DF applies a piezo correction on top.
    ``czi_path`` may be None (no CZI produced): only the ZDrive is logged then.
    """
    czi_z = helper.get_focus_position_from_czi(czi_path) if czi_path else None
    score = helper.compute_focus_score(czi_path)         if czi_path else None
    score_str = f"{score:.1f}" if score is not None else "n/a"
    if czi_z is not None:
        log.info(f"{label}  czi_FocusPos={czi_z:.3f} µm"
                 f"  (DF_offset={czi_z - z_fs_um:+.3f} µm vs zdrive)"
                 f"  focus_score={score_str}")
    else:
        log.info(f"{label}  zdrive={z_fs_um:.3f} µm"
                 f"  (czi_FocusPos not found)  focus_score={score_str}")


# ---------------------------------------------------------------------------
# Per-nucleus and per-position steps
# ---------------------------------------------------------------------------

def image_nucleus(nucleus, tag, df, detailed_folder, log):
    """Detailed pass for one nucleus: move, focus, reference image, confocal z-stack."""
    x, y   = nucleus["abs_x_m"], nucleus["abs_y_m"]
    nuc_id = nucleus["id"]
    what   = f"nucleus {nuc_id:04d}"
    name   = f"{tag}_nucleus_{nuc_id:04d}"
    log.info(f"Moving to nucleus {nuc_id}  x={x:.6f} m  y={y:.6f} m")

    ms.move_stage_to_new_xy_position(x, y)

    # SWAF on the LSM channel — parfocal with the confocal acquisition, so SWAF2
    # sets Z exactly where the confocal will image.  Using DV SWAF here would
    # introduce a ~3 µm parfocality error for the LSM acquisitions.
    z_fs_um = focus_at_current_xy(df, (NUCLEUS_SWAF_EXPERIMENT_PATH, NUCLEUS_SWAF_EXPERIMENT_PATH_2),
                                  what, tag, log)
    if z_fs_um is None:
        raise RuntimeError(f"DefiniteFocus FindSurface failed: {df.last_message}")

    # Single-plane reference image at SWAF2 focus position.  Acquired before the
    # z-stack so we can verify focus quality without waiting for the full stack.
    # Non-fatal if the experiment is missing — the z-stack still runs.
    try:
        ms.run_experiment_from_path(ONCZ_EXPERIMENT_PATH, detailed_folder, f"{name}_oncz")
    except Exception as oncz_err:
        log.warning(f"oncz experiment skipped for nucleus {nuc_id:04d} at {tag}: {oncz_err}")

    # Confocal z-stack, centered on the current focus position (SWAF2).
    # 11 planes × 2 µm = 20 µm total range, ±10 µm around SWAF2.
    zstack_result = ms.run_experiment_from_path(DETAILED_EXPERIMENT_PATH, detailed_folder, name)

    # Use the returned result path (authoritative; accounts for any
    # collision-counter suffix) rather than reconstructing the name.
    czi_path = zstack_result.get("exp_result_path")
    czi_path = Path(czi_path) if czi_path and Path(czi_path).exists() else None
    log_czi_focus(log, f"Detailed image saved: {name}.czi", czi_path, z_fs_um)

    # First/center/last plane Z, to verify the stack is centered on SWAF2.
    zr = helper.get_zstack_z_range(czi_path) if czi_path else None
    if zr:
        log.info(
            f"Z-stack range: first={zr['first_um']:.3f} µm  "
            f"center={zr['center_um']:.3f} µm  "
            f"last={zr['last_um']:.3f} µm  "
            f"({zr['n_z']} planes @ {zr['step_um']:.1f} µm)"
        )


def image_position(pos, well, df, run_path, log):
    """Overview pass at one well position, then detailed imaging of detected nuclei."""
    tag = f"{well}_{pos['position_name']}"   # e.g. "D9_P1" — used in all filenames
    log.info(f"--- Position {tag}  x={pos['x_m']:.6f} m  y={pos['y_m']:.6f} m ---")

    # ── 1. Move XY stage ──────────────────────────────────────────
    try:
        ms.move_stage_to_new_xy_position(pos["x_m"], pos["y_m"])
    except Exception as e:
        log.warning(f"Cannot reach position {tag}: {e} — skipping.")
        return

    # ── 2. Optics, DefiniteFocus FindSurface, SWAF coarse + fine (widefield/DV channel)
    z_fs_um = focus_at_current_xy(df, (SWAF_EXPERIMENT_PATH, SWAF_EXPERIMENT_PATH_2),
                                  "overview", tag, log)
    if z_fs_um is None:
        log.error(f"DefiniteFocus FindSurface failed at {tag}: {df.last_message} — skipping position.")
        return

    # ── 3. Acquire overview image ─────────────────────────────────
    # Use the result path returned by run_experiment instead of globbing
    # the folder for the newest *.czi: globbing is racy (can pick up a
    # leftover file) and misses the collision-counter suffix the
    # acquisition may append. exp_result_path is authoritative.
    ov_result = ms.run_experiment_from_path(OVERVIEW_EXPERIMENT_PATH,
                                            run_path / OVERVIEW_IMAGE_DIRNAME,
                                            f"{tag}_overview")
    image_path = ov_result.get("exp_result_path")
    if image_path is None or not Path(image_path).exists():
        log.error(f"No overview CZI produced for {tag} — skipping.")
        return
    image_path = Path(image_path)
    log_czi_focus(log, f"Overview acquired: {tag}_overview", image_path, z_fs_um)

    # ── 4. Nuclei detection ───────────────────────────────────────
    # Runs the analysis script as a subprocess; outputs a targets.json
    # with one entry per detected target (nucleus here), including
    # absolute XY coords.  `targets.json` is the general analysis
    # contract; the entries happen to be nuclei for this pipeline.
    analysis_path = run_path / ANALYSIS_DIRNAME
    success       = run_analysis(image_path, analysis_path, tag, log,
                                 ANALYSIS_SCRIPT, ANALYSIS_SCRIPT_DIR)
    targets_json  = analysis_path / f"{tag}_targets.json"
    if not success or not targets_json.exists():
        log.warning(f"Analysis produced no targets.json for {tag} — skipping detailed imaging.")
        return

    nuclei = json.loads(targets_json.read_text())
    if not nuclei:
        log.info(f"No nuclei detected at {tag} — moving to next position.")
        return

    # ── 5. Detailed imaging of up to 3 random nuclei (keeps run time predictable)
    targets = random.sample(nuclei, min(3, len(nuclei)))
    log.info(f"{len(nuclei)} nuclei detected — imaging {len(targets)} at random.")
    for nucleus in targets:
        try:
            image_nucleus(nucleus, tag, df, run_path / DETAILED_DIRNAME, log)
        except ms.DefiniteFocusFault:
            raise   # hardware fault: stop the run, don't skip to the next nucleus
        except Exception as e:
            log.warning(f"Skipping nucleus {nucleus['id']} at {tag}: {e}")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main():
    # Per-run output folder: F:/UserData/api/run_YYYYmmdd_HHMMSS/...
    # One timestamp groups this run's images, analysis and log together and keeps
    # separate runs from overwriting each other.
    run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_path      = ROOT_PATH / f"run_{run_timestamp}"

    log, _ = helper.setup_run_logger(run_path / LOG_DIRNAME)
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
    # are not all individually guarded.  Fail fast with a clear list.
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
    log.info(f"Loaded {len(positions)} stage positions from {POSITIONS_FILE.name}")

    # One tracker for the whole run: remembers the last surface Z (adaptive DF
    # start instead of a full 3+ mm sweep from -300 µm) and raises
    # DefiniteFocusFault if DF fails at too many positions in a row.
    df = ms.DefiniteFocusTracker()
    try:
        for well, well_positions in wells_by_name.items():
            log.info(f"=== Well {well} ===")
            for pos in well_positions:
                image_position(pos, well, df, run_path, log)
    except ms.DefiniteFocusFault as e:
        log.error(f"{e} — aborting run.")
        return 1

    log.info("All positions processed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
