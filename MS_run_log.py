# -*- coding: utf-8 -*-

#################################################################
# File        : MS_run_log.py
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
Run records: the per-run log file, the version record and JSONL records.

No ZEN API here. ``MS_Helper_function`` re-exports every name.

Public functions
----------------
setup_run_logger -- Timestamped run log (file + stdout) on the shared logger
write_run_info   -- ``run_info_<ts>.json``: the code and software of a run
append_jsonl     -- Append one record to a crash-safe ``.jsonl`` file
read_jsonl       -- Read the records back (a cut-off last line is skipped)

Constants
---------
DEFAULT_EXPERIMENT_OUTPUT_FOLDER -- where acquisitions go when the caller names
    no folder. Machine-specific: override with the ``SMARTMIC_OUTPUT_ROOT``
    environment variable.
DEFAULT_LOG_FOLDER -- ``<output folder>/log``.
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from MS_zenapi_helpers import (LOG_DATEFMT, LOG_FORMAT, LOGGER_NAME,
                               ensure_utf8_stdout, set_logging)

# Default output folder of an acquisition (the one place this path is written).
# Keep it on the drive of ZEN's own default image folder: the move of each
# result is then a fast rename (DEV_NOTES).
DEFAULT_EXPERIMENT_OUTPUT_FOLDER = Path(
    os.environ.get("SMARTMIC_OUTPUT_ROOT", "F:/UserData/mike/api"))

# Default folder for run log files
DEFAULT_LOG_FOLDER = DEFAULT_EXPERIMENT_OUTPUT_FOLDER / "log"


def setup_run_logger(
    log_folder: Optional[Path] = None,
    name: str = LOGGER_NAME,
) -> Tuple[logging.Logger, Path]:
    """
    Create a timestamped per-run log file and return a logger that emits to
    both the log file and stdout.

    Args:
        log_folder: Directory for the log file.
                    Defaults to DEFAULT_LOG_FOLDER.
        name:       Logger name passed to logging.getLogger().
                    Use different names when running multiple independent
                    scripts in the same process to avoid handler collisions.

    Returns:
        Tuple ``(logger, log_file)`` — the configured ``logging.Logger`` and
        the ``Path`` to the timestamped per-run log file it writes to.

    Note:
        Uses the logger name and line format of ``MS_zenapi_helpers`` (imported,
        one definition).  Call this once at the start of a run to attach the
        per-run file handler; the wrapper modules' ``set_logging`` then returns
        this same configured logger, so their output also lands in the run log.
    """
    if log_folder is None:
        log_folder = DEFAULT_LOG_FOLDER
    log_folder = Path(log_folder)
    log_folder.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file  = log_folder / f"run_{timestamp}.log"

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # Clear any handlers left over from a previous call so we never
    # accumulate duplicates when the function is called more than once.
    logger.handlers.clear()

    fmt = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ensure_utf8_stdout()   # µ etc. must not crash a Windows cp1252 terminal
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info(f"Run log: {log_file}")
    return logger, log_file


def write_run_info(log_file: Path, log: logging.Logger,
                   repos: Optional[Dict[str, Path]] = None,
                   extra: Optional[Dict[str, Any]] = None,
                   script: Optional[Path] = None) -> Path:
    """
    Write the run's version record, ``run_info_<ts>.json``, next to its run log.

    The record pins a dataset to the exact code and software that produced it:
    SmartMic's full commit + uncommitted files, the same for each repo in
    ``repos`` (the project's own repo, its analysis repo(s), …), and the ZEN app,
    ZenApiGateway and ``zen_api`` stub versions (none of which the gRPC API
    reports — see ``zeiss_paths``). ``preflight()`` logs a one-line summary of the
    ZEN/SmartMic versions; this is the full, machine-readable record. Call it
    once per run, right after ``setup_run_logger``. Promoted from Marc_SM_003.

    Every repo's commit is also logged, with a WARNING listing its uncommitted
    files, so a run on modified code is visible in the run log itself.

    Args:
        log_file: The run log from ``setup_run_logger`` (``run_<ts>.log``); the
                  record is written beside it as ``run_info_<ts>.json`` (any
                  other log name ``<stem>.log`` → ``<stem>_run_info.json``).
        log:      Logger for the summary lines.
        repos:    ``{name: path}`` of further git repos to record, e.g.
                  ``{"project": PROJECT_DIR, "analysis": ANALYSIS_DIR}``. A path
                  that is not a git checkout records ``commit: None``.
        extra:    Optional JSON-serialisable dict stored under ``"extra"``
                  (e.g. the run's key settings).
        script:   The script to record; defaults to ``sys.argv[0]``.

    Returns:
        Path of the written JSON file.
    """
    import zeiss_paths   # lazy: keeps this module importable without the Zeiss tree
    import MS_zen_logs

    if script is None and sys.argv and sys.argv[0]:
        script = Path(sys.argv[0]).resolve()
    api_ver, api_path = zeiss_paths.zen_api_version()
    info = {
        "started": datetime.now().isoformat(timespec="seconds"),
        "script": str(script) if script else None,
        "smartmic": {"describe": zeiss_paths.smartmic_version(),
                     **zeiss_paths.git_state(zeiss_paths.THIS_DIR)},
        "repos": {name: {"path": str(path), **zeiss_paths.git_state(path)}
                  for name, path in (repos or {}).items()},
        "zen_app": MS_zen_logs.zen_app_version(),
        "zen_api_gateway": zeiss_paths.zen_api_gateway_version(),
        "zen_api": {"version": api_ver, "path": api_path},
        "python": sys.version.split()[0],
    }
    if extra is not None:
        info["extra"] = extra

    log_file = Path(log_file)
    stem = log_file.stem
    name = ("run_info" + stem[3:]) if stem.startswith("run_") else f"{stem}_run_info"
    path = log_file.with_name(name + ".json")
    path.write_text(json.dumps(info, indent=2, default=str), encoding="utf-8")

    for repo_name, state in [("smartmic", info["smartmic"]), *info["repos"].items()]:
        dirty = state["dirty_files"]
        msg = f"{repo_name} commit {state['commit'] or 'unknown (not a git checkout?)'}"
        if dirty:
            log.warning(f"{msg} — {len(dirty)} UNCOMMITTED file(s): {', '.join(dirty)}")
        else:
            log.info(msg)
    log.info(f"Version record: {path}")
    return path


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    """
    Append one record as a JSON line — the per-record log of a resumable run
    (e.g. one line per imaged cell, read back on resume with read_jsonl).

    Each record is flushed and fsync'ed, so a crash or power cut loses at most
    the line being written. If the file does not end in a newline (the last
    write was cut off), a newline is written first, so the fragment stays on a
    line of its own instead of swallowing this record too.

    Args:
        path:   The .jsonl file (created, with its folder, if absent).
        record: A JSON-serializable dict (numpy scalars are not — convert first).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record) + "\n"   # ASCII-only (ensure_ascii) → a cut can't split a UTF-8 char
    with open(path, "a+b") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() > 0:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                line = "\n" + line
        f.write(line.encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """
    Read the records written by append_jsonl.

    Lines that are not valid JSON — a record cut off by a crash — are skipped
    with a warning; blank lines are ignored.

    Args:
        path: The .jsonl file.

    Returns:
        The records in file order; [] if the file does not exist.
    """
    path = Path(path)
    if not path.exists():
        return []
    records = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            set_logging().warning(
                f"{path.name}: line {n} is not valid JSON (cut off by a crash?) — skipped.")
    return records
