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
Vendored, dependency-free versions of the few ``zen_api_utils`` helpers
SmartMic needs.

``zen_api`` (the auto-generated gRPC stubs) is an installable package, but
``zen_api_utils`` is hand-written example glue that ships only inside the
ZEN-API ``python_examples`` folder and is not packaged.  By vendoring the few
functions actually used here, the SmartMic library depends solely on
``zen_api`` and does not need ``zen_api_utils`` on ``sys.path``.

What is vendored
----------------
* :func:`set_logging`        -- stdlib logger setup (the Zeiss
                                ``misc.set_logging`` uses loguru; see below)
* :func:`initialize_zenapi`  -- config.ini → SSL context → grpclib Channel +
                                control-token metadata (from ``misc.py``)
* :func:`open_zen_channel`   -- async context manager around ``initialize_zenapi``
* :func:`default_rpc_timeout` -- the time limit a ZEN-API call gets when the
                                caller gives none (not in the Zeiss original)
* objective / optovar position lookups (from ``objective.py``)

Only third-party imports (``grpclib``) and the stdlib are used — importing this
module does NOT require the Zeiss tree to be resolvable.

Logging
-------
``set_logging`` returns a plain :mod:`logging` logger (stdlib, not loguru) on the
shared ``"smartmic"`` logger name, the same name used by
:func:`MS_run_log.setup_run_logger`.  This unifies SmartMic onto one
logging stack: when a pipeline (e.g. the PoC) has already called
``setup_run_logger`` to attach a per-run file handler, the wrapper modules'
``set_logging()`` returns that same configured logger, so their output lands in
the run log file too.  When a wrapper module is used standalone, ``set_logging``
attaches a UTF-8 stdout handler so logs still print.
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

# The one logger name and line format of SmartMic. setup_run_logger
# (MS_run_log) imports them, so both configure the SAME logger object.
LOGGER_NAME = "smartmic"
LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(message)s"
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def ensure_utf8_stdout() -> None:
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


def set_logging(name: str = LOGGER_NAME) -> logging.Logger:
    """Return the shared stdlib logger, attaching a stdout handler if needed.

    The whole SmartMic library uses ONE logging stack (stdlib :mod:`logging`);
    the Zeiss original uses loguru.

    Returns ``logging.getLogger(name)``.  If that logger already has handlers —
    e.g. :func:`MS_run_log.setup_run_logger` configured it with a per-run
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
        ensure_utf8_stdout()
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
        logger.addHandler(handler)
    return logger


# ---------------------------------------------------------------------------
# Default time limits for the gRPC calls
# ---------------------------------------------------------------------------
# A call to a gateway that hangs otherwise blocks the pipeline with no message.
# These apply to every call made on a channel from initialize_zenapi /
# open_zen_channel for which the caller gives no ``timeout`` (a caller's own
# timeout always wins). They are a guard against a hang, not a tuned value:
# both are much longer than any duration seen on the CD7.
RPC_TIMEOUT_S: float = 60.0         # reads, settings, load / import / save
MOVE_RPC_TIMEOUT_S: float = 300.0   # stage, Z, objective, optovar, container, DF

# Methods that move hardware (matched on the method name of the route).
_MOVE_RPC_METHODS = frozenset({
    "MoveTo", "MoveToContainer", "FindSurface", "RecallFocus", "LockFocus",
})
# Calls with no default limit: an acquisition has no upper bound, and a
# "Register..." call is a stream that stays open.
_UNLIMITED_RPC_METHODS = frozenset({
    "RunExperiment", "RunSnap", "StartExperiment", "StartSnap", "StartLive",
    "StartContinuous",
})
# Services whose calls get no default: the caller gives the limit itself
# (tray load / eject), or ZEN limits the search (SWAF ``timeout`` field).
_UNLIMITED_RPC_SERVICES = frozenset({"LiveScanService", "ExperimentSwAutofocusService"})


def default_rpc_timeout(route: str) -> Optional[float]:
    """The default time limit (s) for a gRPC route, or None for no limit.

    Args:
        route: e.g. ``"/zen_api.lm.hardware.v2.StageService/MoveTo"``.

    Returns:
        None for an acquisition, a stream, a tray RPC or a SWAF RPC;
        ``MOVE_RPC_TIMEOUT_S`` for a call that moves hardware;
        ``RPC_TIMEOUT_S`` for every other call.
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

    The one place where the limit is applied: the service stubs are used
    unchanged. ``last_route`` / ``last_timeout`` name the latest call, for the
    message of a timeout (``open_zen_channel``).
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
    """Create a gRPC Channel + control-token metadata from a ZEN-API config.ini.

    Vendored from ``zen_api_utils.misc.initialize_zenapi``.  Uses only the
    stdlib (``configparser`` / ``ssl``) plus ``grpclib`` (already a hard
    dependency).  ``cert_file`` is resolved relative to the config file when it
    is given as a relative path.

    Args:
        config_file: Path to the ``config.ini`` file (str or Path).  A relative
            path is resolved against the current working directory.

    Returns:
        Tuple ``(channel, metadata)`` — a :class:`ZenChannel` (a grpclib
        :class:`~grpclib.client.Channel` with default time limits, see
        :func:`default_rpc_timeout`) and the metadata list
        ``[("control-token", <token>)]``.

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

    Cached: every wrapper call opens its own channel (a poll loop does so twice
    a second), and the file and the CA certificate do not change in between.
    ``mtime_ns`` is part of the cache key only, so an edited config.ini is read
    again. A changed certificate FILE at the same path needs a new process.
    """
    config = configparser.ConfigParser()
    config.read(config_path)
    api = config["api"]

    # A TLS client context that trusts the ZEN-API gateway's CA cert.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    # Resolve cert_file relative to the config file when it is a relative path.
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

    Raises:
        TimeoutError: A call did not answer within its time limit
            (:func:`default_rpc_timeout`, or the caller's own timeout). The
            message names the call. ZEN may still carry out the request: a
            timeout stops the wait, not the hardware.
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
# Duck-typed: these only read the ``.objectives`` / ``.optovars`` lists whose
# items expose ``.position``, so no zen_api message-type imports are required.


def get_objective_by_position(objectives: Any, position: int) -> Optional[Any]:
    """Return the objective whose ``.position`` matches *position*, or ``None``.

    Args:
        objectives: an ``ObjectiveChangerServiceGetObjectivesResponse``.
        position:   the position index to look up.
    """
    for obj in objectives.objectives:
        if obj.position == position:
            return obj
    return None


def get_optovar_by_position(optovars: Any, position: int) -> Optional[Any]:
    """Return the optovar whose ``.position`` matches *position*, or ``None``.

    Args:
        optovars: an ``OptovarServiceGetOptovarsResponse``.
        position: the position index to look up.
    """
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
