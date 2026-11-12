"""
The SmartMic proof of concept: make an overview, find nuclei, image some of them.

The carrier is a Multichamber 384. The optics are the 20x/0.95 objective with
the 2x tubelens. At each position in use in POSITIONS_FILE, the pipeline does
these steps:

  1. Move to the position. Focus with DefiniteFocus. The run uses one tracker,
     which has a start point that adapts, and which stops the run after a
     fault. Then run the coarse SWAF and the fine SWAF on the widefield
     channel.
  2. Acquire a widefield overview in DAPI.
  3. Find the nuclei and change them into stage positions, with
     ia_PoC_004/analyze_nuclei.py in its own pixi environment.
  4. For as many as N_NUCLEI of the nuclei, move to the nucleus and focus
     again. The pipeline selects the nuclei at random, but the selection is
     the same for each run and position. The second focus uses DF and the SWAF
     of the LSM channel, because the DV channel and the LSM channel are
     approximately 3 µm apart. Then acquire one confocal plane, which is the
     focus check, and a confocal z-stack of 11 planes 2 µm apart, at the
     centre.

The pipeline writes the record of each position to
``<run>/<run>_positions.jsonl``. A new run with the same RUN_NAME continues the
old run. With RESUME, the pipeline does not do the positions that are complete,
but it does the positions that gave an error. The run stops after
MAX_CONSECUTIVE_ERRORS errors in sequence, or after a DefiniteFocus fault. Each
run writes ``logs/run_info_<timestamp>.json``, which holds the commits of
SmartMic, of the project and of the analysis, and the versions of ZEN.

To start the pipeline, use this command::

    pixi run -e smartmic poc
"""

import os
import random
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# The configuration. Change these values for your run. Lengths are in metres.
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parent
EXPERIMENTS = PROJECT_DIR / "base_experiments"            # Run BY PATH. Git holds them.
POSITIONS_FILE = PROJECT_DIR / "position_files" / "384WP_TestPositions_004.czexp"

EXPECTED_SAMPLE_CARRIER = "Multichamber 384"
OPTICS = (3, 1)                    # The Plan-Apochromat 20x/0.95 and the 2x tubelens.

OVERVIEW_CZEXP = EXPERIMENTS / "DAPI_GFP_001.czexp"                # Widefield DAPI.
OVERVIEW_SWAF = (EXPERIMENTS / "DV_001_swaf_001.czexp",            # The coarse and the
                 EXPERIMENTS / "DV_001_swaf_002.czexp")            # fine SWAF, widefield.
PLANE_CZEXP = EXPERIMENTS / "DAPI_LSM_onez_001.czexp"              # One confocal plane.
ZSTACK_CZEXP = EXPERIMENTS / "DAPI_LSM_z-stack_001.czexp"          # Confocal, 11 x 2 µm, centred.
NUCLEUS_SWAF = (EXPERIMENTS / "DAPI_LSM_onez_001_swaf_001.czexp",  # The coarse and the
                EXPERIMENTS / "DAPI_LSM_onez_001_swaf_002.czexp")  # fine SWAF, LSM.

ANALYSIS_DIR = Path(r"C:\Users\zeiss\Mike\Image_Analysis\ia_PoC_004")   # Own pixi environment.
ANALYSIS_SCRIPT = ANALYSIS_DIR / "analyze_nuclei.py"

RUN_NAME = "run_001"               # A new run with this name continues in the same folder.
OUTPUT_ROOT = Path("F:/UserData/api/SmartMic_PoC")
RUN_FOLDER = OUTPUT_ROOT / RUN_NAME
LOG_FOLDER = RUN_FOLDER / "logs"
RECORDS = RUN_FOLDER / f"{RUN_NAME}_positions.jsonl"

POSITIONS_TO_RUN = None            # For example ["C4_P1", "C5_P2"]. None gives all of them.
N_NUCLEI = 3                       # The number of detailed images for each position.
RESUME = True
FINISHED = {"ok", "no_nuclei"}     # With RESUME, the pipeline does not do these again.
MAX_CONSECUTIVE_ERRORS = 3         # Stop if the hardware has trouble. You can continue later.

# ---------------------------------------------------------------------------

_SMARTMIC_ROOT = os.environ.get("SMARTMIC_ROOT", r"C:\Users\zeiss\Mike\SmartMic")
sys.path.insert(0, _SMARTMIC_ROOT)
import zeiss_paths  # noqa: E402,F401  — Import first. It puts zen_api on sys.path.
import MS_CD7_API_LoA as ms  # noqa: E402
from MS_image_analysis import run_analysis_json  # noqa: E402
from MS_metadata import load_positions_from_czexp  # noqa: E402
from MS_run_log import append_jsonl, read_jsonl, setup_run_logger, write_run_info  # noqa: E402


def focus_here(df, swaf, log):
    """Run DF FindSurface, then the coarse SWAF and the fine SWAF.

    Returns:
        ``{z_df_m, z_swaf_m}``, or ``None`` if DF found no surface. The caller
        then goes to the next position. A SWAF that fails only writes to the
        log, and Z stays where it was.
    """
    z_df = df.find_surface()
    if z_df is None:
        log.warning(f"DF found no surface ({df.last_message})")
        return None
    z = None
    for czexp in swaf:
        try:
            z = ms.swaf_find_focus(czexp)
        except ms.FocusSearchFailed as e:
            log.warning(f"{czexp.stem}: {e} — keeping the previous Z")
    log.info(f"Focus: DF {z_df * 1e6:.2f} µm, SWAF {'—' if z is None else f'{z * 1e6:.2f} µm'}")
    return {"z_df_m": z_df, "z_swaf_m": z}


def image_nucleus(target, tag, folder, df, log):
    """Do the detailed pass at one nucleus.

    The steps are: move to the nucleus, focus with the LSM SWAF, acquire one
    plane, and acquire a z-stack.
    """
    name = f"{tag}_n{target['id']:04d}"
    ms.move_stage_to_new_xy_position(target["x_m"], target["y_m"])
    rec = {"id": target["id"], "x_m": target["x_m"], "y_m": target["y_m"]}
    focus = focus_here(df, NUCLEUS_SWAF, log)
    if focus is None:
        return {**rec, "status": "no_surface"}
    ms.run_experiment_from_path(PLANE_CZEXP, folder, f"{name}_plane")
    zs = ms.run_experiment_from_path(ZSTACK_CZEXP, folder, f"{name}_zstack")
    return {**rec, **focus, "status": "ok", "zstack": str(zs["exp_result_path"])}


def image_position(pos, df, log):
    """Do the overview pass at one position, then the detailed pass at the nuclei.

    The function images as many as N_NUCLEI nuclei.
    """
    tag = f"{pos['well']}_{pos['position_name']}"
    folder = RUN_FOLDER / tag
    rec = {"position": tag, "x_m": pos["x_m"], "y_m": pos["y_m"]}
    ms.move_stage_to_new_xy_position(pos["x_m"], pos["y_m"])
    focus = focus_here(df, OVERVIEW_SWAF, log)
    if focus is None:
        return {**rec, "status": "no_surface"}
    ov = ms.run_experiment_from_path(OVERVIEW_CZEXP, folder, f"{tag}_overview")
    res = run_analysis_json(Path(ov["exp_result_path"]), folder, tag, log,
                            ANALYSIS_SCRIPT, "targets", ANALYSIS_DIR)
    if res is None:
        return {**rec, **focus, "status": "analysis_failed"}
    if not res["found"]:
        return {**rec, **focus, "status": "no_nuclei", "n_nuclei": 0}
    pick = random.Random(f"{RUN_NAME}/{tag}").sample(
        res["targets"], min(N_NUCLEI, len(res["targets"])))
    log.info(f"{tag}: {res['n_nuclei']} nuclei, imaging ids {[t['id'] for t in pick]}")
    nuclei = [image_nucleus(t, tag, folder, df, log) for t in pick]
    return {**rec, **focus, "status": "ok", "n_nuclei": res["n_nuclei"], "nuclei": nuclei}


def main():
    LOG_FOLDER.mkdir(parents=True, exist_ok=True)
    log, log_file = setup_run_logger(LOG_FOLDER)
    log.info(f"SmartMic PoC — {RUN_NAME}")
    write_run_info(log_file, log, repos={"project": PROJECT_DIR, "analysis": ANALYSIS_DIR},
                   extra={"run": RUN_NAME, "positions_file": POSITIONS_FILE.name})

    required = [POSITIONS_FILE, OVERVIEW_CZEXP, PLANE_CZEXP, ZSTACK_CZEXP,
                *OVERVIEW_SWAF, *NUCLEUS_SWAF, ANALYSIS_SCRIPT]
    missing = [p for p in required if not Path(p).exists()]
    for p in missing:
        log.error(f"Missing required input: {p}")
    if missing:
        return 1
    try:
        ms.preflight(EXPECTED_SAMPLE_CARRIER, log)
    except ms.PreflightFailed:
        return 1

    positions = sorted(load_positions_from_czexp(POSITIONS_FILE), key=lambda p: p["scene_index"])
    if POSITIONS_TO_RUN is not None:
        unknown = set(POSITIONS_TO_RUN) - {f"{p['well']}_{p['position_name']}" for p in positions}
        if unknown:
            log.error(f"POSITIONS_TO_RUN not in {POSITIONS_FILE.name}: {sorted(unknown)}")
            return 1
        positions = [p for p in positions
                     if f"{p['well']}_{p['position_name']}" in POSITIONS_TO_RUN]
    done = ({r["position"] for r in read_jsonl(RECORDS) if r.get("status") in FINISHED}
            if RESUME else set())
    todo = [p for p in positions if f"{p['well']}_{p['position_name']}" not in done]
    log.info(f"{len(positions)} positions, {len(positions) - len(todo)} finished "
             f"→ {len(todo)} to do")

    ms.set_optics_verified(*OPTICS)
    df = ms.DefiniteFocusTracker()
    counts, errors_in_a_row = {}, 0
    try:
        for pos in todo:
            log.info(f"--- {pos['well']}_{pos['position_name']} ---")
            try:
                rec = image_position(pos, df, log)
                errors_in_a_row = 0
            except ms.DefiniteFocusFault:
                raise
            except Exception as e:                   # One position must not stop the run.
                log.exception(f"ERROR — {e}")
                rec = {"position": f"{pos['well']}_{pos['position_name']}",
                       "status": "error", "error": repr(e)}
                errors_in_a_row += 1
            rec["finished"] = datetime.now().isoformat(timespec="seconds")
            append_jsonl(RECORDS, rec)
            counts[rec["status"]] = counts.get(rec["status"], 0) + 1
            if errors_in_a_row >= MAX_CONSECUTIVE_ERRORS:
                log.error(f"STOPPED: {errors_in_a_row} positions in a row failed — check the "
                          f"microscope, then re-run unchanged (RESUME continues).")
                return 3
    except ms.DefiniteFocusFault as e:
        log.error(f"STOPPED: {e}")
        return 2
    except BaseException:                            # After a crash or Ctrl-C, ZEN must stop.
        log.exception("Run aborted")
        try:
            ms.stop_experiment()
        except Exception:
            log.exception("Could not stop the experiment")
        raise
    log.info("Done: " + ", ".join(f"{s} {n}" for s, n in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
