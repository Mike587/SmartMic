# -*- coding: utf-8 -*-

#################################################################
# File        : MS_image_analysis.py
# Author      : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#
# Reusable launcher for running external image-analysis scripts as
# subprocesses in their own pixi environments.
#
# Copyright(c) 2026 ETH Zurich (ScopeM). All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

"""
MS_image_analysis.py

Reusable launcher for external image-analysis scripts.

Smart microscopy is the interaction of microscopy and image analysis.  The
microscopy half lives in this repo (the MS_* wrapper modules); the analysis
half is kept as separate, self-contained pixi projects (e.g. the nuclei
detector in the ZEN-API ``image_analysis`` tree) so each can have its own
dependency stack without conflicting with the ZEN API environment.

This module provides the glue that runs such an analysis script as a
subprocess in *its own* pixi environment and feeds it an image, decoupling the
acquisition pipeline from any particular analysis implementation.
"""

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

# run_analysis_json: a result file older than the launch by more than this is a
# stale file from an earlier run. The slack covers coarse file timestamps.
_RESULT_MTIME_SLACK_S = 2.0


def run_analysis(image_path: Path,
                 output_folder: Path,
                 tag: str,
                 log: logging.Logger,
                 analysis_script: Path,
                 analysis_script_dir: Path = None,
                 extra_args=None) -> bool:
    """
    Launch an external image-analysis script in its own pixi environment.

    The analysis script is intentionally run as a subprocess so that its
    dependencies (bioio, scikit-image, scipy, napari, etc.) don't need to be
    installed in the ZEN API environment.  All ``PIXI_*`` environment variables
    are stripped before the subprocess is launched so that the child process
    picks up its own pixi environment rather than inheriting the parent's.

    The script is invoked as::

        pixi run python <analysis_script> <image_path> <output_folder> --prefix <tag> [extra_args...]

    so any analysis project exposing that CLI (image, output dir, ``--prefix``)
    can be plugged in here.

    Args:
        image_path:          Path to the image (e.g. .czi) to analyse.
        output_folder:       Folder where analysis results are written
                             (created if absent).
        tag:                 Filename prefix, e.g. "D9_P1".  Forwarded to
                             ``--prefix``.
        log:                 Run-level logger.
        analysis_script:     Path to the analysis entry-point script.
        analysis_script_dir: Working directory for the subprocess (so pixi
                             resolves the analysis project's environment).
                             Defaults to ``analysis_script.parent``.
        extra_args:          Additional CLI arguments appended after ``--prefix
                             <tag>``, forwarded as-is (stringified).

    Returns:
        True if the analysis script exited with code 0, False otherwise.
    """
    image_path = Path(image_path)
    output_folder = Path(output_folder)
    analysis_script = Path(analysis_script)
    if analysis_script_dir is None:
        analysis_script_dir = analysis_script.parent

    output_folder.mkdir(parents=True, exist_ok=True)

    cmd = [
        "pixi", "run", "python", str(analysis_script),
        str(image_path),
        str(output_folder),
        "--prefix", tag,
    ]
    if extra_args:
        cmd += [str(a) for a in extra_args]

    log.info(f"Starting analysis for {tag}: {image_path.name}")

    # Strip all pixi env vars so the analysis project uses its own environment
    # rather than inheriting the caller's (ZEN API) pixi environment.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIXI_")}

    result = subprocess.run(
        cmd,
        cwd=str(analysis_script_dir),
        capture_output=True,
        text=True,
        env=env,
    )

    if result.stdout:
        for line in result.stdout.strip().splitlines():
            log.info(f"[ANALYSIS] {line}")
    if result.returncode != 0:
        log.error(f"Analysis failed (exit code {result.returncode}):")
        for line in result.stderr.strip().splitlines():
            log.error(f"  {line}")
        return False

    log.info(f"Analysis completed successfully for {tag}.")
    return True


def run_analysis_json(image_path: Path,
                      output_folder: Path,
                      tag: str,
                      log: logging.Logger,
                      analysis_script: Path,
                      suffix: str,
                      analysis_script_dir: Path = None,
                      extra_args=None) -> Optional[dict]:
    """
    Run an analysis (see run_analysis) and read back its result JSON.

    The result is ``<output_folder>/<tag>_<suffix>.json`` — the convention of
    the analysis repos (e.g. suffix ``"targets"`` for analyses built from the
    ia_PoC_002 scaffold; DEV_NOTES "Image-analysis repos").

    Args:
        image_path, output_folder, tag, log, analysis_script,
        analysis_script_dir, extra_args: as for run_analysis.
        suffix: Result-file suffix, e.g. "targets".

    Returns:
        The parsed result, or None (with a warning logged) if the analysis
        failed, wrote no result file, left only a stale one from an earlier run
        (same tag), or wrote invalid JSON.
    """
    result_path = Path(output_folder) / f"{tag}_{suffix}.json"
    script_name = Path(analysis_script).name
    t_start = time.time()
    if not run_analysis(image_path, output_folder, tag, log, analysis_script,
                        analysis_script_dir, extra_args=extra_args):
        log.warning(f"Analysis {script_name} failed for {tag} — no result.")
        return None
    if not result_path.is_file():
        log.warning(f"Analysis {script_name} wrote no {result_path.name} — no result.")
        return None
    if result_path.stat().st_mtime < t_start - _RESULT_MTIME_SLACK_S:
        log.warning(f"{result_path.name} was not rewritten by {script_name} (stale file "
                    f"from an earlier run) — no result.")
        return None
    try:
        return json.loads(result_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        log.warning(f"{result_path.name} is not valid JSON ({e}) — no result.")
        return None
