# -*- coding: utf-8 -*-

#################################################################
# Based on    : zen_api_utils/misc.py
# Author      : SRh, JSm
# Institution : Carl Zeiss Microscopy GmbH
#
# Copyright(c) 2025 Carl Zeiss AG, Germany. All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

#################################################################
# File        : MS_logging.py
# Modified by : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#################################################################

"""The shared logger of SmartMic.

Each module gets the logger with ``set_logging``. ``MS_run_log.setup_run_logger``
adds the log file of a run to the same logger. This module uses only the
standard library. Thus you can import it anywhere.
"""

import logging
import sys

# MS_run_log.setup_run_logger uses these names too. Both configure the SAME
# logger.
LOGGER_NAME = "smartmic"
LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(message)s"
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def ensure_utf8_stdout() -> None:
    """Try to make ``sys.stdout`` UTF-8, for example to print µ on a cp1252 console.

    The function calls ``reconfigure`` on the stream. It does not make a new
    ``TextIOWrapper``, because a new wrapper closes the shared buffer when
    Python releases it.
    """
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def set_logging(name: str = LOGGER_NAME) -> logging.Logger:
    """Get the shared logger of the standard library.

    If the logger has no handler, the function attaches a UTF-8 stdout handler.
    If ``MS_run_log.setup_run_logger`` set the logger up before, the function
    returns it with no change. You can call this function more than once.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        ensure_utf8_stdout()
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
        logger.addHandler(handler)
    return logger
