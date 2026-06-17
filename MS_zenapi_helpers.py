# -*- coding: utf-8 -*-

#################################################################
# Based on    : zen_api_utils/misc.py, zen_api_utils/objective.py
# Author      : SRh, JSm
# Institution : Carl Zeiss Microscopy GmbH
#
# Copyright(c) 2025 Carl Zeiss AG, Germany. All Rights Reserved.
#
# Permission is granted to use, modify and distribute this code,
# as long as this copyright notice remains part of the code.
#################################################################

#################################################################
# File        : MS_zenapi_helpers.py
# Modified by : Michael Stebler
# Institution : ETH Zurich | ScopeM
#               ScopeM Imaging Facility (scopem.ethz.ch)
#################################################################

"""
Vendored, dependency-free replacements for the handful of ``zen_api_utils``
helpers SmartMic relied on.

``zen_api`` (the auto-generated gRPC stubs) is an installable package, but
``zen_api_utils`` is hand-written example glue that ships only inside the
ZEN-API ``python_examples`` folder and is not packaged.  By vendoring the few
functions actually used here, the SmartMic library depends solely on
``zen_api`` and no longer needs ``zen_api_utils`` on ``sys.path``.

What is vendored
----------------
* :func:`set_logging`        -- stdlib logger setup (replaces the loguru-based
                                ``misc.set_logging``; see below)
* :func:`initialize_zenapi`  -- config.ini → SSL context → grpclib Channel +
                                control-token metadata (from ``misc.py``)
* :func:`open_zen_channel`   -- async context manager around ``initialize_zenapi``
* objective / optovar position lookups (from ``objective.py``)

Only third-party imports (``grpclib``) and the stdlib are used — importing this
module does NOT require the Zeiss tree to be resolvable.

Logging
-------
``set_logging`` returns a plain :mod:`logging` logger (NOT loguru anymore) on the
shared ``"smartmic"`` logger name, the same name used by
:func:`MS_Helper_function.setup_run_logger`.  This unifies SmartMic onto one
logging stack: when a pipeline (e.g. the PoC) has already called
``setup_run_logger`` to attach a per-run file handler, the wrapper modules'
``set_logging()`` returns that same configured logger, so their output lands in
the run log file too.  When a wrapper module is used standalone, ``set_logging``
attaches a UTF-8 stdout handler so logs still print.
"""

import configparser
import logging
import ssl
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Tuple, Union

from grpclib.client import Channel

# Shared with MS_Helper_function.setup_run_logger so both configure/return the
# SAME logger object.  Keep the format in sync with that function.
_LOGGER_NAME = "smartmic"
_LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(message)s"
_LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def _ensure_utf8_stdout() -> None:
    """Best-effort: make ``sys.stdout`` encode UTF-8 so characters like µ don't
    crash on a Windows cp1252 console.

    Uses ``reconfigure`` (Python 3.7+) to mutate the existing stream in place
    rather than wrapping it in a new owning ``TextIOWrapper`` — wrapping would
    close the shared underlying buffer when the wrapper is dropped, breaking any
    other handler on stdout.  No-op if stdout cannot be reconfigured.
    """
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def set_logging(name: str = _LOGGER_NAME) -> logging.Logger:
    """Return the shared stdlib logger, attaching a stdout handler if needed.

    Replaces the previous loguru-based helper so the whole SmartMic library uses
    ONE logging stack (stdlib :mod:`logging`).

    Returns ``logging.getLogger(name)``.  If that logger already has handlers —
    e.g. :func:`MS_Helper_function.setup_run_logger` configured it with a per-run
    file handler — it is returned untouched, so wrapper-module logs flow into the
    same run log.  Otherwise a UTF-8-wrapped stdout handler is attached (so
    standalone use of a wrapper module still prints, and special characters like
    µ don't crash a Windows cp1252 terminal).  Idempotent: repeated calls never
    accumulate duplicate handlers.

    Returns:
        logging.Logger: the shared, handler-equipped logger.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        _ensure_utf8_stdout()
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
        logger.addHandler(handler)
    return logger


def initialize_zenapi(
    config_file: Union[str, Path] = "config.ini",
) -> Tuple[Channel, List[Tuple[str, str]]]:
    """Create a gRPC Channel + control-token metadata from a ZEN-API config.ini.

    Vendored from ``zen_api_utils.misc.initialize_zenapi``.  Uses only the
    stdlib (``configparser`` / ``ssl``) plus ``grpclib`` (already a hard
    dependency).  ``cert_file`` is resolved relative to the config file when it
    is given as a relative path.

    Args:
        config_file: Path to the ``config.ini`` file (str or Path).  A relative
            path is resolved against the current working directory.

    Returns:
        Tuple ``(channel, metadata)`` — a grpclib :class:`~grpclib.client.Channel`
        and the metadata list ``[("control-token", <token>)]``.

    Raises:
        FileNotFoundError: if the configuration file does not exist.
    """
    config_path = Path(config_file).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    config = configparser.ConfigParser()
    config.read(config_path)

    # Build a TLS client context that trusts the ZEN-API gateway's CA cert.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    # Resolve cert_file relative to the config file when it is a relative path.
    cert_file = Path(config["api"]["cert_file"])
    if not cert_file.is_absolute():
        cert_file = (config_path.parent / cert_file).resolve()

    context.load_verify_locations(cafile=str(cert_file))
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    # ZEN-API speaks HTTP/2 (h2) over TLS.
    context.set_alpn_protocols(["h2"])

    channel = Channel(
        host=config["api"]["host"], port=int(config["api"]["port"]), ssl=context
    )
    metadata = [("control-token", config["api"]["control-token"])]
    return channel, metadata


@asynccontextmanager
async def open_zen_channel(config_file: Union[str, Path] = "config.ini"):
    """Async context manager: open a ZEN-API gRPC channel and ALWAYS close it.

    Wraps :func:`initialize_zenapi` and guarantees ``channel.close()`` runs on
    exit, whether the ``async with`` body completes normally or raises.  This is
    the single place channel lifetime is managed, so the wrapper functions no
    longer leak a channel when a gRPC call fails mid-function.

    Usage::

        async with open_zen_channel(config_path) as (channel, metadata):
            svc = SomeServiceStub(channel=channel, metadata=metadata)
            ...   # any raise here still closes the channel

    Args:
        config_file: Path to the ``config.ini`` (forwarded to
            :func:`initialize_zenapi`).

    Yields:
        Tuple ``(channel, metadata)`` — the open :class:`~grpclib.client.Channel`
        and the control-token metadata list.
    """
    channel, metadata = initialize_zenapi(config_file)
    try:
        yield channel, metadata
    finally:
        channel.close()


# ---------------------------------------------------------------------------
# Objective / optovar position lookups (from zen_api_utils.objective)
# ---------------------------------------------------------------------------
# Duck-typed: these only read the ``.objectives`` / ``.optovars`` lists whose
# items expose ``.position``, so no zen_api message-type imports are required.


def get_objective_by_position(objectives, position: int):
    """Return the objective whose ``.position`` matches *position*, or ``None``.

    Args:
        objectives: an ``ObjectiveChangerServiceGetObjectivesResponse``.
        position:   the position index to look up.
    """
    for obj in objectives.objectives:
        if obj.position == position:
            return obj
    return None


def get_optovar_by_position(optovars, position: int):
    """Return the optovar whose ``.position`` matches *position*, or ``None``.

    Args:
        optovars: an ``OptovarServiceGetOptovarsResponse``.
        position: the position index to look up.
    """
    for opt in optovars.optovars:
        if opt.position == position:
            return opt
    return None


def get_used_objective_positions(objectives) -> List[int]:
    """Return the list of all objective positions reported by the hardware."""
    return [obj.position for obj in objectives.objectives]


def get_used_optovar_positions(optovars) -> List[int]:
    """Return the list of all optovar positions reported by the hardware."""
    return [opt.position for opt in optovars.optovars]
