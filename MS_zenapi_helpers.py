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
* :func:`set_logging`        -- loguru logger config (from ``misc.py``)
* :func:`initialize_zenapi`  -- config.ini → SSL context → grpclib Channel +
                                control-token metadata (from ``misc.py``)
* objective / optovar position lookups (from ``objective.py``)

These reproduce the upstream behaviour exactly so the wrapper modules did not
have to change beyond their import lines.  Only third-party imports
(``grpclib``, ``loguru``) and the stdlib are used — importing this module does
NOT require the Zeiss tree to be resolvable.
"""

import configparser
import ssl
import sys
from pathlib import Path
from typing import List, Tuple, Union

from grpclib.client import Channel
from loguru import logger


def set_logging():
    """Configure and return a loguru logger writing colourised lines to stdout.

    Vendored verbatim from ``zen_api_utils.misc.set_logging`` so SmartMic no
    longer imports it.  Existing handlers are removed first, so repeated calls
    do not accumulate duplicate sinks.

    Returns:
        loguru.Logger: the configured logger instance.
    """
    logger.remove()
    logger.add(
        sys.stdout,
        colorize=True,
        format="<green>{time}s</green> - <level>{level}</level> - <level>{message}</level>",
    )
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
