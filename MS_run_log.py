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
The records of a run: the log file, the version record and the JSONL records.

This module uses no ZEN API. ``DEFAULT_EXPERIMENT_OUTPUT_FOLDER`` is different
on each machine. To change it, set the ``SMARTMIC_OUTPUT_ROOT`` environment
variable.
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

# Keep this folder on the drive of the default image folder of ZEN. The move
# of each result is then a fast rename (DEV_NOTES).
DEFAULT_EXPERIMENT_OUTPUT_FOLDER = Path(
    os.environ.get("SMARTMIC_OUTPUT_ROOT", "F:/UserData/mike/api"))

DEFAULT_LOG_FOLDER = DEFAULT_EXPERIMENT_OUTPUT_FOLDER / "log"


def setup_run_logger(
    log_folder: Optional[Path] = None,
    name: str = LOGGER_NAME,
) -> Tuple[logging.Logger, Path]:
    """
    Attach a ``run_<timestamp>.log`` file and stdout to the shared logger.

    Call this function one time at the start of a run. The ``set_logging``
    function of the wrappers then returns this logger. Thus the output of the
    wrappers also goes into the run log. The function replaces the handlers
    that are already there.

    Args:
        log_folder: The default is DEFAULT_LOG_FOLDER.
        name:       The name of the logger. Use a different name for a
                    separate script in the same process.

    Returns:
        ``(logger, log_file)``.
    """
    if log_folder is None:
        log_folder = DEFAULT_LOG_FOLDER
    log_folder = Path(log_folder)
    log_folder.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_folder / f"run_{timestamp}.log"

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # Prevent two sets of handlers if you call this function again.
    logger.handlers.clear()

    fmt = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ensure_utf8_stdout()   # A character such as µ must not stop a cp1252 terminal.
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
    Write the version record of the run next to its log file.

    The name of the record is ``run_info_<timestamp>.json``. The record holds
    the commit and the files that are not committed, for SmartMic and for each
    repository in ``repos``. It also holds the versions of the ZEN
    application, of ZenApiGateway and of ``zen_api``. The gRPC API reports none
    of these versions. See ``zeiss_paths``. The function writes the commit of
    each repository to the log, and a WARNING for the files that are not
    committed. Call this function one time for each run, directly after
    ``setup_run_logger``.

    Args:
        log_file: The ``run_<timestamp>.log`` file from ``setup_run_logger``.
                  A different ``<stem>.log`` gives ``<stem>_run_info.json``.
        repos:    ``{name: path}`` of the other git repositories. A path that
                  is not a git checkout records ``commit: None``.
        extra:    A dict that JSON can write. It goes under ``"extra"``.
        script:   The default is ``sys.argv[0]``.

    Returns:
        The path of the JSON file that the function wrote.
    """
    # Import these here, not at the top of the module. Thus you can import
    # this module without the Zeiss tree.
    import zeiss_paths
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
    Add one record to a file as a JSON line.

    This gives a log of a run that you can continue after a crash. The function
    flushes the data and calls fsync. Thus a crash loses only the line that the
    function writes at that moment. If the last line in the file is not
    complete, the function writes a newline first. Thus the incomplete line
    does not take in this record.

    Args:
        path:   The function makes the file and its folder if they do not
                exist.
        record: A dict that JSON can write. Change numpy scalars to Python
                values first.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # json.dumps uses ensure_ascii, thus the line holds ASCII only. A cut
    # cannot divide a UTF-8 character.
    line = json.dumps(record) + "\n"
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
    Read the records that ``append_jsonl`` wrote.

    If a line is not valid, because a crash cut it, the function writes a
    warning and ignores that line.

    Returns:
        The records in the order of the file. If the file does not exist, the
        function returns ``[]``.
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
