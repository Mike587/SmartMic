# -*- coding: utf-8 -*-

#################################################################
# File        : MS_FocusSanityCheck.py
# Author      : Mike Stebler
# Institution : ETH Zurich | ScopeM
#
# Standalone focus sanity-check.
#
# Park the stage at any position you want to inspect, then run:
#
#   python MS_FocusSanityCheck.py
#
# Sequence:
#   1. DefiniteFocus FindSurface  — full sweep from -300 µm
#   2. SWAF1 (coarse)
#   3. SWAF2 (fine)
#
# Every result is logged to stdout and a timestamped log file.
# No images are acquired.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent))

try:
    import MS_CD7_API_LoA as ms
    import MS_Helper_function as helper
except ImportError as e:
    print(f"[ERROR] Could not import modules: {e}")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SWAF_EXPERIMENT_NAME   = "DAPI_LSM_onez_001_swaf_001"
SWAF_EXPERIMENT_NAME_2 = "DAPI_LSM_onez_001_swaf_002"
IMAGE_EXPERIMENT_NAME   = "DAPI_LSM_onez_001"
ZSTACK_EXPERIMENT_NAME  = "DAPI_LSM_z-stack_001"
IMAGE_FOLDER            = Path("F:/UserData/mike/api/detailed")
LOG_FOLDER             = Path("F:/UserData/mike/api/log")
# ---------------------------------------------------------------------------


def run_focus_sanity_check():
    log, log_file = helper.setup_run_logger(LOG_FOLDER, name="focus_sanity")
    log.info("=== Focus sanity check — stage must already be at target position ===")

    # ------------------------------------------------------------------
    # Step 1: DefiniteFocus FindSurface
    # ------------------------------------------------------------------
    log.info("Step 1 — DefiniteFocus FindSurface (start Z = −300 µm) ...")
    success, msg, df_attempts = ms.run_definite_focus_find_surface()
    z_df_um = ms.get_current_z_position() * 1e6
    if success:
        log.info(f"  FindSurface : SUCCESS ({df_attempts} attempt(s))")
    else:
        log.warning(f"  FindSurface : FAILED ({df_attempts} attempt(s)) — {msg}")
    log.info(f"  ZDrive after FindSurface : {z_df_um:.3f} µm")

    # ------------------------------------------------------------------
    # Step 2: SWAF1
    # ------------------------------------------------------------------
    log.info(f"Step 2 — SWAF1 ({SWAF_EXPERIMENT_NAME}) ...")
    z_swaf1, swaf1_attempts = ms.run_swaf(SWAF_EXPERIMENT_NAME)
    if z_swaf1 is not None:
        log.info(f"  SWAF1 : SUCCESS ({swaf1_attempts} attempt(s))")
        log.info(f"  SWAF1 focus : {z_swaf1:.3f} µm"
                 f"  (FindSurface→SWAF1: {z_df_um - z_swaf1:+.3f} µm)")
    else:
        log.warning(f"  SWAF1 : FAILED ({swaf1_attempts} attempt(s))")

    # ------------------------------------------------------------------
    # Step 3: SWAF2
    # ------------------------------------------------------------------
    log.info(f"Step 3 — SWAF2 ({SWAF_EXPERIMENT_NAME_2}) ...")
    z_swaf2, swaf2_attempts = ms.run_swaf(SWAF_EXPERIMENT_NAME_2)
    if z_swaf2 is not None:
        ref = z_swaf1 if z_swaf1 is not None else z_df_um
        log.info(f"  SWAF2 : SUCCESS ({swaf2_attempts} attempt(s))")
        log.info(f"  SWAF2 focus : {z_swaf2:.3f} µm"
                 f"  (SWAF1→SWAF2: {ref - z_swaf2:+.3f} µm)")
    else:
        log.warning(f"  SWAF2 : FAILED ({swaf2_attempts} attempt(s))")

    # ------------------------------------------------------------------
    # Step 4: Acquire image
    # ------------------------------------------------------------------
    from datetime import datetime
    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    image_name = f"sanity_{timestamp}"
    log.info(f"Step 4 — Acquiring {IMAGE_EXPERIMENT_NAME} → {image_name} ...")
    try:
        IMAGE_FOLDER.mkdir(parents=True, exist_ok=True)
        ms.run_experiment(IMAGE_EXPERIMENT_NAME, IMAGE_FOLDER, image_name, False)
        log.info(f"  Image saved: {IMAGE_FOLDER / image_name}.czi")
    except Exception as e:
        log.error(f"  Image acquisition failed: {e}")

    # ------------------------------------------------------------------
    # Step 5: Z-stack
    # ------------------------------------------------------------------
    zstack_name = f"sanity_zstack_{timestamp}"
    log.info(f"Step 5 — Acquiring {ZSTACK_EXPERIMENT_NAME} → {zstack_name} ...")
    try:
        ms.run_experiment(ZSTACK_EXPERIMENT_NAME, IMAGE_FOLDER, zstack_name, False)
        zstack_czi = IMAGE_FOLDER / f"{zstack_name}.czi"
        log.info(f"  Z-stack saved: {zstack_czi}")
        if zstack_czi.exists():
            score = helper.compute_focus_score(zstack_czi)
            zr    = helper.get_zstack_z_range(zstack_czi)
            score_str = f"{score:.1f}" if score is not None else "n/a"
            if zr:
                log.info(f"  Z-stack range: first={zr['first_um']:.3f} µm  "
                         f"center={zr['center_um']:.3f} µm  "
                         f"last={zr['last_um']:.3f} µm  "
                         f"({zr['n_z']} planes @ {zr['step_um']:.1f} µm)  "
                         f"focus_score={score_str}")
    except Exception as e:
        log.error(f"  Z-stack acquisition failed: {e}")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    log.info("--- Summary ---")
    log.info(f"  FindSurface (ZDrive) : {z_df_um:.3f} µm")
    log.info(f"  SWAF1 focus          : {f'{z_swaf1:.3f} µm' if z_swaf1 is not None else 'n/a'}")
    log.info(f"  SWAF2 focus          : {f'{z_swaf2:.3f} µm' if z_swaf2 is not None else 'n/a'}")
    if z_swaf2 is not None:
        log.info(f"  Total drift (FindSurface → SWAF2) : {z_df_um - z_swaf2:+.3f} µm")
    log.info(f"Log saved to: {log_file}")


if __name__ == "__main__":
    run_focus_sanity_check()
