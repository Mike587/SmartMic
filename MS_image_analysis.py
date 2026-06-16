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

import logging
import os
import subprocess
from pathlib import Path


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

        pixi run python <analysis_script> <image_path> <output_folder> --prefix <tag>

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
