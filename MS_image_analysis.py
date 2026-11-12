# -*- coding: utf-8 -*-

#################################################################
# File        : MS_image_analysis.py
# Author      : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#
# Copyright(c) 2026 ETH Zurich (ScopeM). All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

"""
Launcher for the external image-analysis scripts.

Each analysis is a different pixi project with its own dependencies. This
module runs the analysis as a subprocess in ITS OWN pixi environment, and
gives it an image (DEV_NOTES "Image-analysis repos").
"""

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# run_analysis_json uses this value. A result file that is older than the
# launch by more than this value is an old file from an earlier run. The
# tolerance covers file timestamps that are not accurate.
_RESULT_MTIME_SLACK_S = 2.0


def run_analysis(image_path: Path,
                 output_folder: Path,
                 tag: str,
                 log: logging.Logger,
                 analysis_script: Path,
                 analysis_script_dir: Optional[Path] = None,
                 extra_args: Optional[List[str]] = None) -> bool:
    """
    Launch an external image-analysis script in its own pixi environment.

    The function removes the ``PIXI_*`` environment variables. Thus the child
    process uses its own environment. The function runs this command::

        pixi run python <analysis_script> <image_path> <output_folder>
            --prefix <tag> [extra_args...]

    Args:
        output_folder:       The function makes this folder if it does not
                             exist.
        tag:                 The prefix of the file name (``--prefix``), for
                             example "D9_P1".
        analysis_script_dir: The working directory of the subprocess. Pixi
                             needs it to find the analysis project. The
                             default is ``analysis_script.parent``.
        extra_args:          The function adds these after ``--prefix <tag>``
                             as strings.

    Returns:
        ``True`` if the script stopped with exit code 0. If not, ``False``.
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

    # Do not inherit the pixi environment of the caller.
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
                      analysis_script_dir: Optional[Path] = None,
                      extra_args: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """
    Run an analysis with ``run_analysis``, then read its JSON result.

    The result file is ``<output_folder>/<tag>_<suffix>.json``. An example of a
    suffix is ``"targets"`` (DEV_NOTES "Image-analysis repos"). The other
    arguments are the arguments of ``run_analysis``.

    Returns:
        The result, or ``None``. The function writes a warning and returns
        ``None`` in these conditions: the analysis failed, it wrote no result
        file, it left only an old file from an earlier run with the same tag,
        or it wrote JSON that is not valid.
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
