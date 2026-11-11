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

"""Vendored ``zen_api_utils`` helpers: logging, channel setup, RPC time limits,
objective / optovar lookups.

Stdlib + ``grpclib`` only: importing this does NOT need the Zeiss tree.
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

# Shared with MS_run_log.setup_run_logger: both configure the SAME logger.
LOGGER_NAME = "smartmic"
LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(message)s"
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def ensure_utf8_stdout() -> None:
    """Best-effort: make ``sys.stdout`` UTF-8 (µ on a cp1252 console).

    ``reconfigure`` in place, not a new ``TextIOWrapper`` (that would close the
    shared buffer when dropped).
    """
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def set_logging(name: str = LOGGER_NAME) -> logging.Logger:
    """Return the shared stdlib logger; attach a UTF-8 stdout handler if it has none.

    A logger already set up by ``MS_run_log.setup_run_logger`` is returned
    untouched. Idempotent.
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
# Default time limits for the gRPC calls
# ---------------------------------------------------------------------------
# Applied to every call without a caller ``timeout`` (the caller's always wins).
# A guard against a hung gateway, not a tuned value (DEV_NOTES "Default time
# limits for the gRPC calls").
RPC_TIMEOUT_S: float = 60.0         # reads, settings, load / import / save
MOVE_RPC_TIMEOUT_S: float = 300.0   # stage, Z, objective, optovar, container, DF

# Methods that move hardware (matched on the method name of the route).
_MOVE_RPC_METHODS = frozenset({
    "MoveTo", "MoveToContainer", "FindSurface", "RecallFocus", "LockFocus",
})
# No default limit: acquisitions are unbounded; "Register..." calls are streams.
_UNLIMITED_RPC_METHODS = frozenset({
    "RunExperiment", "RunSnap", "StartExperiment", "StartSnap", "StartLive",
    "StartContinuous",
})
# No default: the caller sets the limit (tray), or ZEN does (SWAF ``timeout``).
_UNLIMITED_RPC_SERVICES = frozenset({"LiveScanService", "ExperimentSwAutofocusService"})


def default_rpc_timeout(route: str) -> Optional[float]:
    """The default time limit (s) for a route such as
    ``"/zen_api.lm.hardware.v2.StageService/MoveTo"``.

    Returns:
        None for an acquisition, a stream, a tray or a SWAF RPC;
        ``MOVE_RPC_TIMEOUT_S`` for a hardware move; else ``RPC_TIMEOUT_S``.
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
    """A grpclib Channel that gives each call a default time limit.

    ``last_route`` / ``last_timeout`` name the latest call, for the timeout
    message in ``open_zen_channel``.
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
    """Create a :class:`ZenChannel` + control-token metadata from a config.ini.

    A relative *config_file* is resolved against the CWD; a relative
    ``cert_file`` against the config file.

    Returns:
        ``(channel, [("control-token", <token>)])``.

    Raises:
        FileNotFoundError: if the configuration file does not exist.
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
    """Parse config.ini and build the TLS context: ``(host, port, token, context)``.

    Cached (every call opens a channel). ``mtime_ns`` is only a cache key, so an
    edited config.ini is re-read; a changed certificate file needs a new process.
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
    # ZEN-API speaks HTTP/2 (h2) over TLS.
    context.set_alpn_protocols(["h2"])

    return api["host"], int(api["port"]), api["control-token"], context


@asynccontextmanager
async def open_zen_channel(config_file: Union[str, Path] = "config.ini",
                           ) -> AsyncIterator[Tuple[Channel, List[Tuple[str, str]]]]:
    """Async context manager: yield ``(channel, metadata)``, ALWAYS close the channel.

    Raises:
        TimeoutError: A call did not answer within its time limit
            (:func:`default_rpc_timeout` or the caller's). The message names
            the call. ZEN may still carry out the request: a timeout stops the
            wait, not the hardware.
    """
    channel, metadata = initialize_zenapi(config_file)
    try:
        yield channel, metadata
    except asyncio.TimeoutError as e:
        if "ZEN-API gateway" in str(e):     # already named by an inner channel
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
# Objective / optovar position lookups (from zen_api_utils.objective)
# ---------------------------------------------------------------------------
# Duck-typed on ``.objectives`` / ``.optovars`` items with ``.position``.


def get_objective_by_position(objectives: Any, position: int) -> Optional[Any]:
    """Return the objective at *position* in a GetObjectives response, or ``None``."""
    for obj in objectives.objectives:
        if obj.position == position:
            return obj
    return None


def get_optovar_by_position(optovars: Any, position: int) -> Optional[Any]:
    """Return the optovar at *position* in a GetOptovars response, or ``None``."""
    for opt in optovars.optovars:
        if opt.position == position:
            return opt
    return None


def get_used_objective_positions(objectives: Any) -> List[int]:
    """Return the list of all objective positions reported by the hardware."""
    return [obj.position for obj in objectives.objectives]


def get_used_optovar_positions(optovars: Any) -> List[int]:
    """Return the list of all optovar positions reported by the hardware."""
    return [opt.position for opt in optovars.optovars]
