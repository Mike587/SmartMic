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

"""Helper functions copied from ``zen_api_utils``.

This module gives the logging set-up, the channel set-up, the default RPC time
limits, and the objective and optovar lookups. It uses only the standard
library and ``grpclib``. Thus an import of this module does NOT need the Zeiss
tree.
"""

import asyncio
import configparser
import functools
import logging
import ssl
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, List, Optional, Tuple, Union

from grpclib.client import Channel

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


# ---------------------------------------------------------------------------
# The default time limits for the gRPC calls
# ---------------------------------------------------------------------------
# These limits apply to each call for which the caller gives no ``timeout``.
# A ``timeout`` from the caller always wins. The limits are a guard against a
# gateway that does not answer. They are not tuned values (DEV_NOTES "Default
# time limits for the gRPC calls").
RPC_TIMEOUT_S: float = 60.0         # reads, settings, load / import / save
MOVE_RPC_TIMEOUT_S: float = 300.0   # stage, Z, objective, optovar, container, DF

# These methods move hardware. The code matches the method name of the route.
_MOVE_RPC_METHODS = frozenset({
    "MoveTo", "MoveToContainer", "FindSurface", "RecallFocus", "LockFocus",
})
# These methods get no default limit. An acquisition has no known duration,
# and a "Register..." call is a stream.
_UNLIMITED_RPC_METHODS = frozenset({
    "RunExperiment", "RunSnap", "StartExperiment", "StartSnap", "StartLive",
    "StartContinuous",
})
# These services get no default limit. For the tray, the caller sets the limit.
# For SWAF, ZEN sets it from its own ``timeout`` argument.
_UNLIMITED_RPC_SERVICES = frozenset({"LiveScanService", "ExperimentSwAutofocusService"})


def default_rpc_timeout(route: str) -> Optional[float]:
    """Get the default time limit in seconds for one route.

    An example of a route is
    ``"/zen_api.lm.hardware.v2.StageService/MoveTo"``.

    Returns:
        ``None`` for an acquisition, a stream, a tray RPC or a SWAF RPC.
        ``MOVE_RPC_TIMEOUT_S`` for a hardware move. ``RPC_TIMEOUT_S`` for all
        the other routes.
    """
    service_path, _, method = route.rpartition("/")
    service = service_path.rsplit(".", 1)[-1]
    if (service in _UNLIMITED_RPC_SERVICES or method in _UNLIMITED_RPC_METHODS
            or method.startswith("Register")):
        return None
    if method in _MOVE_RPC_METHODS:
        return MOVE_RPC_TIMEOUT_S
    return RPC_TIMEOUT_S


class ZenChannel(Channel):
    """A grpclib ``Channel`` that gives each call a default time limit.

    ``last_route`` and ``last_timeout`` hold the data of the most recent call.
    ``open_zen_channel`` uses them in its timeout message.
    """

    last_route: Optional[str] = None
    last_timeout: Optional[float] = None

    def request(self, name: str, cardinality: Any, request_type: Any, reply_type: Any, *,
                timeout: Optional[float] = None, deadline: Any = None,
                metadata: Any = None) -> Any:
        if timeout is None and deadline is None:
            timeout = default_rpc_timeout(name)
        self.last_route, self.last_timeout = name, timeout
        return super().request(name, cardinality, request_type, reply_type,
                               timeout=timeout, deadline=deadline, metadata=metadata)


def initialize_zenapi(
    config_file: Union[str, Path] = "config.ini",
) -> Tuple[Channel, List[Tuple[str, str]]]:
    """Make a :class:`ZenChannel` and the control-token metadata from a config.ini.

    If *config_file* is a relative path, the function resolves it against the
    working directory. If ``cert_file`` is a relative path, the function
    resolves it against the configuration file.

    Returns:
        ``(channel, [("control-token", <token>)])``.

    Raises:
        FileNotFoundError: The configuration file does not exist.
    """
    config_path = Path(config_file).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    host, port, token, context = _connection_settings(
        str(config_path), config_path.stat().st_mtime_ns)
    return ZenChannel(host=host, port=port, ssl=context), [("control-token", token)]


@functools.lru_cache(maxsize=8)
def _connection_settings(config_path: str, mtime_ns: int,
                         ) -> Tuple[str, int, str, ssl.SSLContext]:
    """Read config.ini and make the TLS context.

    The function caches the result, because each call opens a channel.
    ``mtime_ns`` is only a cache key. Thus the function reads a config.ini that
    you edited again. A changed certificate file needs a new process.

    Returns:
        ``(host, port, token, context)``.
    """
    config = configparser.ConfigParser()
    config.read(config_path)
    api = config["api"]

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    cert_file = Path(api["cert_file"])
    if not cert_file.is_absolute():
        cert_file = (Path(config_path).parent / cert_file).resolve()

    context.load_verify_locations(cafile=str(cert_file))
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    # ZEN-API uses HTTP/2 (h2) on TLS.
    context.set_alpn_protocols(["h2"])

    return api["host"], int(api["port"]), api["control-token"], context


@asynccontextmanager
async def open_zen_channel(config_file: Union[str, Path] = "config.ini",
                           ) -> AsyncIterator[Tuple[Channel, List[Tuple[str, str]]]]:
    """Give ``(channel, metadata)``, and ALWAYS close the channel at the end.

    This function is an asynchronous context manager.

    Raises:
        TimeoutError: A call did not answer in its time limit. The limit comes
            from :func:`default_rpc_timeout` or from the caller. The message
            gives the name of the call. ZEN can still do the request, because
            a timeout stops the wait, not the hardware.
    """
    channel, metadata = initialize_zenapi(config_file)
    try:
        yield channel, metadata
    except asyncio.TimeoutError as e:
        if "ZEN-API gateway" in str(e):     # An inner channel named it already.
            raise
        route = getattr(channel, "last_route", None) or "a ZEN-API call"
        limit = getattr(channel, "last_timeout", None)
        raise type(e)(
            f"No answer from the ZEN-API gateway: {route} did not finish within "
            f"{f'{limit:.0f} s' if limit else 'its time limit'}. ZEN may still "
            f"carry out the request (a timeout stops the wait, not the hardware)."
            f"{f' [{e}]' if str(e) else ''}"
        ) from e
    finally:
        channel.close()


# ---------------------------------------------------------------------------
# The objective and optovar position lookups, from zen_api_utils.objective
# ---------------------------------------------------------------------------
# These functions accept any object that has an ``.objectives`` or
# ``.optovars`` list whose items have a ``.position``.


def get_objective_by_position(objectives: Any, position: int) -> Optional[Any]:
    """Get the objective at *position* in a GetObjectives response, or ``None``."""
    for obj in objectives.objectives:
        if obj.position == position:
            return obj
    return None


def get_optovar_by_position(optovars: Any, position: int) -> Optional[Any]:
    """Get the optovar at *position* in a GetOptovars response, or ``None``."""
    for opt in optovars.optovars:
        if opt.position == position:
            return opt
    return None


def get_used_objective_positions(objectives: Any) -> List[int]:
    """Get the list of all the objective positions that the hardware reports."""
    return [obj.position for obj in objectives.objectives]


def get_used_optovar_positions(optovars: Any) -> List[int]:
    """Get the list of all the optovar positions that the hardware reports."""
    return [opt.position for opt in optovars.optovars]
