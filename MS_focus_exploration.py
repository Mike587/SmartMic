# -*- coding: utf-8 -*-

#################################################################
# File        : MS_focus_exploration.py
# Author      : Mike Stebler
# Institution : ETH Zurich | ScopeM
#
# Two entry-points:
#
#   test_DF_vs_swaf(...)
#       Run N cycles of DF FindSurface + SWAF at the current position,
#       stepping the objective down by step_um after each cycle.
#       Saves a 2-panel plot (Z positions + DF-SWAF delta) next to the log.
#
#   test_multiposition_DF_vs_swaf(...)
#       Visit every position in a plate position list, run one DF + SWAF
#       measurement at each, and produce a 3-panel per-well analysis figure.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

import asyncio
import logging
import re
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from grpclib import GRPCError

import zeiss_paths  # noqa: F401  — extends sys.path so zen_api / zen_api_utils resolve
import MS_Helper_function as helper
import MS_zenapi_stage_LM
from zen_api_utils.misc import initialize_zenapi

from zen_api.acquisition.v1beta import (
    ExperimentServiceStub,
    ExperimentServiceLoadRequest,
)
from zen_api.lm.hardware.v2 import (
    FocusServiceGetPositionRequest,
    FocusServiceMoveToRequest,
    FocusServiceStub,
)
from zen_api.lm.acquisition.v1 import (
    DefiniteFocusServiceStub,
    DefiniteFocusServiceFindSurfaceRequest,
    DefiniteFocusServiceStoreFocusRequest,
    ExperimentSwAutofocusServiceStub,
    ExperimentSwAutofocusServiceFindAutoFocusRequest,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
config_path              = Path(__file__).parent / "config.ini"
POSITIONS_FILE           = Path("F:/UserData/mike/api/positions/384WP_TestPositions_001_positions.json")
SWAF_EXPERIMENT_NAME     = "DV_001_swaf_001"
IMAGING_EXPERIMENT_NAME  = "DV_001"
DETAILED_FOLDER          = Path("F:/UserData/mike/api/detailed")
N_CYCLES                 = 10
STEP_UM                  = 8.0
SWAF_TIMEOUT_S           = 30
# ---------------------------------------------------------------------------


# ===========================================================================
# CZI metadata helper
# ===========================================================================

_FOCUS_POS_RE  = re.compile(rb"<FocusPosition>([\+\-]?[\d.]+)</FocusPosition>")
_CZI_READ_BYTES = 8 * 1024 * 1024   # CZI metadata always precedes pixel data


def get_focus_position_from_czi(czi_path: Path) -> float | None:
    """
    Return the FocusPosition (µm) stored in a CZI file's XML metadata,
    or None if the tag is absent or the file cannot be read.
    """
    try:
        with open(czi_path, "rb") as f:
            chunk = f.read(_CZI_READ_BYTES)
        m = _FOCUS_POS_RE.search(chunk)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


# ===========================================================================
# Graph helpers
# ===========================================================================

def _save_single_position_graph(
    results: list[dict], graph_path: Path,
    n_cycles: int, step_um: float, swaf_name: str,
    log: logging.Logger,
) -> None:
    """2-panel: Z positions per cycle + DF-SWAF delta bar chart."""
    cycles    = [r["cycle"]  for r in results]
    df_vals   = [r["df_z"]   if r["df_z"]   is not None else np.nan for r in results]
    swaf_vals = [r["swaf_z"] if r["swaf_z"] is not None else np.nan for r in results]
    delta     = [d - s for d, s in zip(df_vals, swaf_vals)]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
    fig.suptitle(f"Focus exploration — {n_cycles} cycles, step {step_um} µm", fontsize=13)

    ax1.plot(cycles, df_vals,   "o-", color="steelblue",  label="Definite Focus (FindSurface)")
    ax1.plot(cycles, swaf_vals, "s-", color="darkorange", label=f"SWAF ({swaf_name})")
    ax1.set_ylabel("Z position (µm)")
    ax1.legend(framealpha=0.7)
    ax1.grid(True, alpha=0.3)
    ax1.set_title("DF and SWAF focus positions")

    colors = ["tomato" if v < 0 else "steelblue" for v in delta]
    ax2.bar(cycles, delta, color=colors, alpha=0.8, zorder=3)
    ax2.axhline(0, color="black", linewidth=0.8, linestyle="--")
    ax2.set_xlabel("Cycle")
    ax2.set_ylabel("DF - SWAF (µm)")
    ax2.set_title("Difference: DF minus SWAF")
    ax2.grid(True, alpha=0.3, axis="y")
    ax2.set_xticks(cycles)

    plt.tight_layout()
    fig.savefig(graph_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log.info(f"Graph saved: {graph_path}")


def _save_multiposition_graph(
    results: list[dict], graph_path: Path, log: logging.Logger
) -> None:
    """
    3-panel well analysis:
      Panel 1 (top, full width) — DF and SWAF absolute Z at every position,
                                   colour-coded by well.
      Panel 2 (bottom-left)    — Mean DF-SWAF delta per well with std-dev
                                   error bars.
      Panel 3 (bottom-right)   — Strip chart: individual deltas per well.
    """
    wells_ordered = list(dict.fromkeys(r["well"] for r in results))   # preserve order
    palette       = plt.cm.tab10.colors
    well_color    = {w: palette[i % 10] for i, w in enumerate(wells_ordered)}

    # ---- collect per-well data ----
    well_df_z   = defaultdict(list)
    well_swaf_z = defaultdict(list)
    well_delta  = defaultdict(list)
    for r in results:
        w = r["well"]
        if r["df_z"] is not None:
            well_df_z[w].append(r["df_z"])
        if r["swaf_z"] is not None:
            well_swaf_z[w].append(r["swaf_z"])
        if r["delta"] is not None:
            well_delta[w].append(r["delta"])

    fig = plt.figure(figsize=(14, 9))
    fig.suptitle("Multi-position DF vs SWAF analysis", fontsize=14, fontweight="bold")
    gs  = fig.add_gridspec(2, 2, hspace=0.38, wspace=0.32,
                           height_ratios=[1.1, 1])
    ax_top   = fig.add_subplot(gs[0, :])   # full-width top
    ax_bar   = fig.add_subplot(gs[1, 0])   # bottom-left
    ax_strip = fig.add_subplot(gs[1, 1])   # bottom-right

    # ------------------------------------------------------------------
    # Panel 1: absolute Z per position, colour = well
    # ------------------------------------------------------------------
    pos_idx = list(range(1, len(results) + 1))
    for i, r in enumerate(results):
        c = well_color[r["well"]]
        ax_top.plot(pos_idx[i], r["df_z"], "o", color=c, markersize=8, zorder=3)
        if r["swaf_z"] is not None:
            ax_top.plot(pos_idx[i], r["swaf_z"], "s", color=c, alpha=0.6, markersize=8, zorder=3)

    # Connect positions within the same well
    well_positions = defaultdict(list)
    for i, r in enumerate(results):
        well_positions[r["well"]].append((pos_idx[i], r["df_z"], r["swaf_z"]))
    for w, pts in well_positions.items():
        xs      = [p[0] for p in pts]
        df_y    = [p[1] for p in pts]
        sw_pts  = [(p[0], p[2]) for p in pts if p[2] is not None]
        c       = well_color[w]
        ax_top.plot(xs, df_y, "-", color=c, linewidth=1.5, label=f"{w} DF")
        if sw_pts:
            ax_top.plot([p[0] for p in sw_pts], [p[1] for p in sw_pts],
                        "--", color=c, alpha=0.6, linewidth=1.5, label=f"{w} SWAF")

    # Custom legend: one entry per well (marker only) + DF/SWAF shape legend
    from matplotlib.lines import Line2D
    well_handles = [Line2D([0], [0], color=well_color[w], lw=2, label=w)
                    for w in wells_ordered]
    shape_handles = [
        Line2D([0], [0], marker="o", color="gray", lw=0,  markersize=8, label="DF"),
        Line2D([0], [0], marker="s", color="gray", lw=0,  markersize=8, alpha=0.6, label="SWAF"),
    ]
    ax_top.legend(handles=well_handles + shape_handles, ncol=len(wells_ordered) + 2,
                  fontsize=8, framealpha=0.7, loc="upper right")
    ax_top.set_xlabel("Position index")
    ax_top.set_ylabel("Z position (µm)")
    ax_top.set_title("DF and SWAF focus Z at each plate position")
    ax_top.set_xticks(pos_idx)
    ax_top.set_xticklabels(
        [f"{r['well']}\n{r['position_name']}" for r in results],
        fontsize=7,
    )
    ax_top.grid(True, alpha=0.25)

    # ------------------------------------------------------------------
    # Panel 2: mean ± std delta per well
    # ------------------------------------------------------------------
    x_pos   = np.arange(len(wells_ordered))
    means   = [np.mean(well_delta[w]) for w in wells_ordered]
    stds    = [np.std(well_delta[w])  for w in wells_ordered]
    colors  = [well_color[w] for w in wells_ordered]
    bars = ax_bar.bar(x_pos, means, color=colors, alpha=0.8, zorder=3,
                      yerr=stds, capsize=5, error_kw={"elinewidth": 1.5})
    grand_mean = np.mean([d for ds in well_delta.values() for d in ds])
    ax_bar.axhline(grand_mean, color="black", linewidth=1, linestyle="--",
                   label=f"Grand mean: {grand_mean:.2f} µm")
    ax_bar.set_xticks(x_pos)
    ax_bar.set_xticklabels(wells_ordered)
    ax_bar.set_ylabel("DF - SWAF (µm)")
    ax_bar.set_title("Mean DF-SWAF delta per well  (± SD)")
    ax_bar.legend(fontsize=8)
    ax_bar.grid(True, alpha=0.25, axis="y")
    # annotate bars with mean ± std
    for xi, (m, s) in enumerate(zip(means, stds)):
        ax_bar.text(xi, m + s + 0.02, f"{m:.2f}±{s:.2f}", ha="center",
                    va="bottom", fontsize=7.5)

    # ------------------------------------------------------------------
    # Panel 3: strip chart — individual deltas per well
    # ------------------------------------------------------------------
    rng = np.random.default_rng(0)
    for xi, w in enumerate(wells_ordered):
        deltas = well_delta[w]
        jitter = rng.uniform(-0.15, 0.15, len(deltas))
        ax_strip.scatter(xi + jitter, deltas, color=well_color[w],
                         s=60, zorder=3, alpha=0.85)
        ax_strip.plot([xi - 0.3, xi + 0.3], [np.mean(deltas)] * 2,
                      color="black", linewidth=2)
    ax_strip.set_xticks(range(len(wells_ordered)))
    ax_strip.set_xticklabels(wells_ordered)
    ax_strip.set_ylabel("DF - SWAF (µm)")
    ax_strip.set_title("Individual deltas per well  (— = mean)")
    ax_strip.grid(True, alpha=0.25, axis="y")

    fig.savefig(graph_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    log.info(f"Analysis graph saved: {graph_path}")


# ===========================================================================
# Core test functions
# ===========================================================================

async def test_DF_vs_swaf(
    focus_service: FocusServiceStub,
    df_service: DefiniteFocusServiceStub,
    exp_service: ExperimentServiceStub,
    swaf_service: ExperimentSwAutofocusServiceStub,
    log: logging.Logger,
    log_file: Path,
    swaf_experiment_name: str = SWAF_EXPERIMENT_NAME,
    n_cycles: int = N_CYCLES,
    step_um: float = STEP_UM,
    swaf_timeout_s: int = SWAF_TIMEOUT_S,
) -> list[dict]:
    """
    Run n_cycles of Definite Focus FindSurface followed by SWAF at the
    current stage position, lowering the objective by step_um after each cycle.

    Returns a list of per-cycle dicts (cycle, df_z µm, swaf_z µm).
    Saves a 2-panel plot alongside log_file.
    """
    log.info(f"Loading SWAF experiment: {swaf_experiment_name} ...")
    exp = await exp_service.load(
        ExperimentServiceLoadRequest(experiment_name=swaf_experiment_name)
    )

    results = []

    for i in range(n_cycles):
        cycle = i + 1
        log.info(f"=== Cycle {cycle}/{n_cycles} ===")

        z_start = await focus_service.get_position(FocusServiceGetPositionRequest())
        log.info(f"  ZDrive start            : {z_start.value * 1e6:.3f} µm")

        fs_resp = await df_service.find_surface(DefiniteFocusServiceFindSurfaceRequest())
        df_z_um = fs_resp.zposition * 1e6
        log.info(f"  DF zposition            : {df_z_um:.3f} µm")
        await df_service.store_focus(DefiniteFocusServiceStoreFocusRequest())

        swaf_z_um = None
        try:
            swaf_resp = await swaf_service.find_auto_focus(
                ExperimentSwAutofocusServiceFindAutoFocusRequest(
                    experiment_id=exp.experiment_id,
                    timeout=swaf_timeout_s,
                )
            )
            swaf_z_um = swaf_resp.focus_position
            log.info(f"  SWAF focus position     : {swaf_z_um:.3f} µm")
            log.info(f"  DF-SWAF delta           : {df_z_um - swaf_z_um:+.3f} µm")
        except GRPCError as e:
            log.error(f"  SWAF failed: {e.message}")

        results.append({"cycle": cycle, "df_z": df_z_um, "swaf_z": swaf_z_um})

        z_current = await focus_service.get_position(FocusServiceGetPositionRequest())
        new_z_m   = z_current.value - step_um * 1e-6
        log.info(f"  Lowering objective {step_um} µm -> {new_z_m * 1e6:.3f} µm")
        await focus_service.move_to(FocusServiceMoveToRequest(value=new_z_m))

    graph_path = log_file.with_name(log_file.stem + "_focus_exploration.png")
    _save_single_position_graph(results, graph_path, n_cycles, step_um,
                                swaf_experiment_name, log)
    return results


async def test_multiposition_DF_vs_swaf(
    focus_service: FocusServiceStub,
    df_service: DefiniteFocusServiceStub,
    exp_service: ExperimentServiceStub,
    swaf_service: ExperimentSwAutofocusServiceStub,
    positions: list[dict],
    log: logging.Logger,
    log_file: Path,
    swaf_experiment_name: str = SWAF_EXPERIMENT_NAME,
    swaf_timeout_s: int = SWAF_TIMEOUT_S,
) -> list[dict]:
    """
    Visit every position in *positions*, run one DF FindSurface + SWAF
    measurement at each, and save a 3-panel per-well analysis figure.

    *positions* is the flat list returned by helper.load_positions_from_json().

    Returns a list of per-position result dicts:
        well, position_name, x_m, y_m, df_z (µm), swaf_z (µm), delta (µm)
    """
    log.info(f"Loading SWAF experiment: {swaf_experiment_name} ...")
    exp = await exp_service.load(
        ExperimentServiceLoadRequest(experiment_name=swaf_experiment_name)
    )

    results = []

    for pos in positions:
        well = pos["well"]
        tag  = f"{well}_{pos['position_name']}"
        log.info(f"=== {tag}  x={pos['x_m']*1e3:.3f} mm  y={pos['y_m']*1e3:.3f} mm ===")

        # Move to position — Z is lowered to 0 automatically before XY move
        await MS_zenapi_stage_LM.move_stage_to_new_xy_position(pos["x_m"], pos["y_m"])

        # DF FindSurface
        fs_resp = await df_service.find_surface(DefiniteFocusServiceFindSurfaceRequest())
        df_z_um = fs_resp.zposition * 1e6
        await df_service.store_focus(DefiniteFocusServiceStoreFocusRequest())
        log.info(f"  DF zposition  : {df_z_um:.3f} µm")

        # SWAF
        swaf_z_um = None
        try:
            swaf_resp = await swaf_service.find_auto_focus(
                ExperimentSwAutofocusServiceFindAutoFocusRequest(
                    experiment_id=exp.experiment_id,
                    timeout=swaf_timeout_s,
                )
            )
            swaf_z_um = swaf_resp.focus_position
            delta     = df_z_um - swaf_z_um
            log.info(f"  SWAF position : {swaf_z_um:.3f} µm  (DF-SWAF: {delta:+.3f} µm)")
        except GRPCError as e:
            log.error(f"  SWAF failed: {e.message}")
            delta = None

        results.append({
            "well":          well,
            "position_name": pos["position_name"],
            "x_m":           pos["x_m"],
            "y_m":           pos["y_m"],
            "df_z":          df_z_um,
            "swaf_z":        swaf_z_um,
            "delta":         delta,
        })

    # Print per-well summary to log
    well_deltas = defaultdict(list)
    for r in results:
        if r["delta"] is not None:
            well_deltas[r["well"]].append(r["delta"])
    log.info("--- Per-well summary ---")
    for w, ds in well_deltas.items():
        log.info(f"  {w}: mean={np.mean(ds):.3f} µm  std={np.std(ds):.3f} µm  n={len(ds)}")

    graph_path = log_file.with_name(log_file.stem + "_multipos_analysis.png")
    _save_multiposition_graph(results, graph_path, log)

    return results


async def test_focus_consistency(
    focus_service: FocusServiceStub,
    df_service: DefiniteFocusServiceStub,
    exp_service: ExperimentServiceStub,
    swaf_service: ExperimentSwAutofocusServiceStub,
    positions: list[dict],
    log: logging.Logger,
    log_file: Path,
    swaf_experiment_name: str = SWAF_EXPERIMENT_NAME,
    imaging_experiment_name: str = IMAGING_EXPERIMENT_NAME,
    output_folder: Path = DETAILED_FOLDER,
    swaf_timeout_s: int = SWAF_TIMEOUT_S,
) -> list[dict]:
    """
    For each well in *positions*, visit the P2 position, run DF FindSurface,
    run SWAF, and then acquire a full experiment image saved to *output_folder*.

    Only P2 positions are visited — P1 and P3 are skipped.

    Returns a list of per-well result dicts:
        well, tag, x_m, y_m, df_z (µm), swaf_z (µm), czi_path, czi_focus_pos (µm)
    """
    # Lazy import to avoid module-level side effects in experiment_methods.py
    import MS_zenapi_experiment_methods as exp_methods

    # Filter to P2 only, one entry per well (preserve plate order)
    p2_positions = [p for p in positions if p["position_name"] == "P2"]
    if not p2_positions:
        log.error("No P2 positions found in position list — aborting test_focus_consistency.")
        return []

    log.info(f"test_focus_consistency: {len(p2_positions)} P2 position(s) found.")
    log.info(f"  SWAF experiment  : {swaf_experiment_name}")
    log.info(f"  Image experiment : {imaging_experiment_name}")
    log.info(f"  Output folder    : {output_folder}")

    log.info(f"Loading SWAF experiment: {swaf_experiment_name} ...")
    exp = await exp_service.load(
        ExperimentServiceLoadRequest(experiment_name=swaf_experiment_name)
    )

    output_folder = Path(output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)

    results = []

    for pos in p2_positions:
        well = pos["well"]
        tag  = f"{well}_P2"
        log.info(f"=== {tag}  x={pos['x_m']*1e3:.3f} mm  y={pos['y_m']*1e3:.3f} mm ===")

        # ---- Move to position ----
        try:
            await MS_zenapi_stage_LM.move_stage_to_new_xy_position(pos["x_m"], pos["y_m"])
        except Exception as e:
            log.warning(f"Cannot reach {tag}: {e} -- skipping.")
            continue

        # ---- Definite Focus FindSurface ----
        fs_resp = await df_service.find_surface(DefiniteFocusServiceFindSurfaceRequest())
        df_z_um = fs_resp.zposition * 1e6
        await df_service.store_focus(DefiniteFocusServiceStoreFocusRequest())
        log.info(f"  DF zposition  : {df_z_um:.3f} µm")

        # ---- SWAF ----
        swaf_z_um = None
        try:
            swaf_resp = await swaf_service.find_auto_focus(
                ExperimentSwAutofocusServiceFindAutoFocusRequest(
                    experiment_id=exp.experiment_id,
                    timeout=swaf_timeout_s,
                )
            )
            swaf_z_um = swaf_resp.focus_position
            log.info(f"  SWAF position : {swaf_z_um:.3f} µm"
                     f"  (DF-SWAF: {df_z_um - swaf_z_um:+.3f} µm)")
        except GRPCError as e:
            log.error(f"  SWAF failed: {e.message}")

        # ---- Acquire image ----
        czi_path    = None
        czi_z_um    = None
        log.info(f"  Acquiring image ({imaging_experiment_name}) -> {tag}.czi ...")
        try:
            acq = await exp_methods.check_experiment_api(
                experiment_name=imaging_experiment_name,
                configfile=str(config_path),
                custom_image_folder=output_folder,
                custom_filename=tag,
                do_snap_and_live=False,
            )
            czi_path = acq.get("exp_result_path")
            if czi_path is not None:
                czi_path = Path(czi_path)
                czi_z_um = get_focus_position_from_czi(czi_path)
                if czi_z_um is not None:
                    log.info(f"  Image saved   : {czi_path.name}"
                             f"  czi_FocusPos={czi_z_um:.3f} µm"
                             f"  (vs DF: {czi_z_um - df_z_um:+.3f} µm)")
                else:
                    log.info(f"  Image saved   : {czi_path.name}"
                             f"  (FocusPosition tag not found in CZI metadata)")
        except Exception as e:
            log.error(f"  Acquisition failed for {tag}: {e}")

        results.append({
            "well":          well,
            "tag":           tag,
            "x_m":           pos["x_m"],
            "y_m":           pos["y_m"],
            "df_z":          df_z_um,
            "swaf_z":        swaf_z_um,
            "czi_path":      czi_path,
            "czi_focus_pos": czi_z_um,
        })

    # Summary table
    log.info("--- test_focus_consistency summary ---")
    for r in results:
        swaf_str = f"{r['swaf_z']:.3f}" if r["swaf_z"] is not None else "n/a"
        czi_str  = f"{r['czi_focus_pos']:.3f}" if r["czi_focus_pos"] is not None else "n/a"
        log.info(f"  {r['tag']:10s}  DF={r['df_z']:.3f} µm"
                 f"  SWAF={swaf_str} µm  CZI_FocusPos={czi_str} µm")

    log.info("test_focus_consistency complete.")
    return results


# ===========================================================================
# Entry point
# ===========================================================================

async def main() -> None:
    log, log_file = helper.setup_run_logger()
    channel, metadata = initialize_zenapi(config_path)

    focus_service = FocusServiceStub(channel=channel, metadata=metadata)
    df_service    = DefiniteFocusServiceStub(channel=channel, metadata=metadata)
    exp_service   = ExperimentServiceStub(channel=channel, metadata=metadata)
    swaf_service  = ExperimentSwAutofocusServiceStub(channel=channel, metadata=metadata)

    positions = helper.load_positions_from_json(POSITIONS_FILE)

    await test_focus_consistency(
        focus_service, df_service, exp_service, swaf_service,
        positions, log, log_file,
    )

    channel.close()


if __name__ == "__main__":
    asyncio.run(main())
