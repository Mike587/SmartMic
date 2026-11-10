# -*- coding: utf-8 -*-

#################################################################
# File        : MS_zen_logs.py
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
Read facts from ZEN's own log and options files that the ZEN-API does not give.

Read-only and best-effort: every reader returns None (or a dict with None
entries) when its source is missing, rotated out or unreadable. Nothing here
talks to the gateway or changes a ZEN setting. The files are in
``zeiss_paths.ZEN_LOGGING_DIR``. Split out of ``zeiss_paths`` on 2026-11-10;
``zeiss_paths.<name>`` still resolves every name here.

Public functions
----------------
zen_app_version                -- Version of the running ZEN application
zen_auto_immersion_settings    -- Auto-immersion refill settings (UI file + service)
format_auto_immersion_settings -- One log line for them; flags a mismatch
zen_user_options_path          -- The logged-in ZEN user's Options.config
zen_service_immersion_options  -- The immersion options ZENService last received
zen_df_bottom                  -- Carrier bottom (material, thickness) DF last received
"""

import os
import re
from pathlib import Path, PureWindowsPath

import zeiss_paths


_SOFTWARE_VERSION_RE = re.compile(r'name="SoftwareVersion"\s+value="([^"]+)"')
_ZEN_LOG_TAIL_BYTES = 65536


def zen_app_version():
    """Best-effort version of the running ZEN APPLICATION itself, for logging.

    Confirmed unavailable over the gRPC API (see ``zeiss_paths.zen_api_version``): none
    of the 27 service stubs expose a version/about/system-info call, so this
    reads it out-of-band, from ZEN's own log. ``ZEN_LOGGING_DIR / "ZEN.log.xml"``
    is NOT one well-formed XML document — it's a flat stream of one <event>...
    </event> fragment per line, no wrapping root element — but ZEN stamps a
    ``SoftwareVersion`` attribute on the <properties> of essentially every event
    it logs. Only the file's TAIL is read (it can be several MB and grows for as
    long as ZEN runs), and the LAST match in that tail is used, so a version that
    changed mid-file (a ZEN restart/upgrade) reflects what's running now, not
    what was running when the file was created.

    Returns the version string (e.g. ``"3.13.109.08000"``), or ``None`` if the
    log file is missing/unreadable or no ``SoftwareVersion`` is found in the tail.
    """
    try:
        text = _tail(zeiss_paths.ZEN_LOGGING_DIR / "ZEN.log.xml", _ZEN_LOG_TAIL_BYTES)
    except OSError:
        return None
    return _last_software_version(text)


def _tail(path, n_bytes):
    """Return the last ``n_bytes`` bytes of ``path``, decoded as UTF-8 (lossy).

    Seeks from the end instead of reading the whole file, since the ZEN logs
    this is used against can be multi-megabyte and only the most recent content
    is ever needed.
    """
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - n_bytes))
        data = f.read()
    return data.decode("utf-8", errors="ignore")


def _last_software_version(text):
    """Pure text parse: the last ``SoftwareVersion="..."`` attribute in ``text``.

    Split out from ``zen_app_version`` so the parsing logic is unit-testable
    against synthetic log text, without needing a real ZEN log file on disk.
    """
    matches = _SOFTWARE_VERSION_RE.findall(text)
    return matches[-1] if matches else None


# ---------------------------------------------------------------------------
# Auto-immersion (re-immersion) settings: read-only, out-of-band.
#
# Not available over the ZEN-API (no options/immersion service; checked with
# gateway reflection, DEV_NOTES "The re-immersion settings are NOT reachable
# through the ZEN-API"). Two sources:
#   * the logged-in ZEN user's Options.config: what the ZEN UI holds;
#   * the last SetServiceOptions request in the ZENService log: what ZEN.exe
#     sent to ZENService at startup, i.e. what the refiller actually uses.
# They can differ if the option was changed in the UI after ZEN started
# (whether ZEN re-sends it then is untested). Nothing here changes a setting.
# ---------------------------------------------------------------------------

# The keys ZEN.exe sends to ZENService (SetServiceOptions payload), in log order.
# Interval in minutes, travel distance in mm, the durations in ms.
AUTO_IMMERSION_KEYS = (
    "IsAutoImmersionEnabled",
    "IsAutoImmersionRefillGlobalEnabled",
    "IsAutoImmersionRefillForExperimentEnabled",
    "IsAutoImmersionRefillTravelDistanceEnabled",
    "AutoImmersionRefillInterval",
    "AutoImmersionRefillTravelDistance",
    "AutoImmersionRemovalPumpDuration",
    "AutoImmersionFeedPumpDuration",
    "AutoImmersionWaitingDuration",
)

# Upper bound on log files scanned (newest first). Each is ~10 MB; a long ZEN
# session can push its startup entries far back. Past this bound the reader
# gives up (returns None) rather than make preflight slow.
_ZEN_LOG_MAX_FILES = 40

# "userOptionsFileName = '...'" at ZEN startup, "Successfully written options
# config file '...'" whenever ZEN saves its options (the workgroup file and the
# user's file).
_OPTIONS_PATH_RE = re.compile(
    r"(?:userOptionsFileName = |written options config file )'([^']+Options\.config)'"
)
_SERVICE_OPTIONS_RE = re.compile(
    r'timestamp="([^"]+)"[^<]*<message><!\[CDATA\[StreamRequestAsync\(\): Receiving '
    r'ServiceRequest: <ServiceRequest Id="SetServiceOptions"[^>]*>(.*?)</ServiceRequest>',
    re.DOTALL,
)
_PAYLOAD_ENTRY_RE = re.compile(r"<(\w+)>([^<]*)</\1>")
_OPTION_ENTRY_RE = re.compile(r'<OptionEntry\s+Name="([^"]+)"\s+Value="([^"]*)"')


def _coerce_option_value(text):
    """'true'/'True' -> bool, '120' -> int, '0.5' -> float, else the string."""
    low = text.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    return text


def _immersion_options_from_options_config(text):
    """Pure parse: the AUTO_IMMERSION_KEYS present in Options.config text.

    Options.config is ``<Options><OptionEntry Name=".." Value=".." />...``.
    Returns a dict with only the keys found (empty if none).
    """
    found = {name: _coerce_option_value(value)
             for name, value in _OPTION_ENTRY_RE.findall(text)}
    return {k: found[k] for k in AUTO_IMMERSION_KEYS if k in found}


def _user_options_path_from_zen_log(text):
    """Pure parse: the last USER Options.config path named in ZEN.log.xml text.

    Skips the workgroup file (``...\\ZEN\\Options.config``) and the installation
    default (under Program Files). What is left is the per-user file,
    ``...\\ZEN\\Users\\<user>\\...`` or ``...\\ZEN\\DefaultUser\\...``.
    Returns the path string or None.
    """
    paths = [p for p in _OPTIONS_PATH_RE.findall(text)
             if ("\\Users\\" in p or "\\DefaultUser\\" in p)
             and "Program Files" not in p]
    return paths[-1] if paths else None


def _last_service_immersion_options(text):
    """Pure parse: the last SetServiceOptions payload that carries immersion keys.

    ZEN sends several SetServiceOptions requests at startup; only one has the
    immersion keys. Returns ``(options_dict, timestamp)`` or ``(None, None)``.
    """
    for timestamp, body in reversed(_SERVICE_OPTIONS_RE.findall(text)):
        entries = dict(_PAYLOAD_ENTRY_RE.findall(body))
        opts = {k: _coerce_option_value(entries[k])
                for k in AUTO_IMMERSION_KEYS if k in entries}
        if opts:
            return opts, timestamp
    return None, None


def _zen_logs_newest_first(stem):
    """``ZEN_LOGGING_DIR/<stem>`` and its rotations (``<stem>.N``), newest first."""
    try:
        files = [f for f in zeiss_paths.ZEN_LOGGING_DIR.glob(f"{stem}*")
                 if f.name == stem or f.name[len(stem):].lstrip(".").isdigit()]
    except OSError:
        return []

    def mtime(f):
        try:
            return f.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(files, key=mtime, reverse=True)[:_ZEN_LOG_MAX_FILES]


def _search_zen_logs(stem, marker, parse):
    """Newest-first scan: ``parse(text)`` of the first log file containing ``marker``.

    A cheap byte search picks the file, so only that one is decoded and parsed.
    Returns None if no scanned file has a hit.
    """
    for path in _zen_logs_newest_first(stem):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if marker in data:
            return parse(data.decode("utf-8", errors="ignore"))
    return None


def zen_user_options_path():
    """Best-effort path of the logged-in ZEN user's Options.config, or None.

    Read from ZEN.log.xml: ZEN names the file when it starts and whenever it
    saves its options.
    """
    found = _search_zen_logs("ZEN.log.xml", b"Options.config",
                             _user_options_path_from_zen_log)
    return Path(found) if found else None


def zen_service_immersion_options():
    """Best-effort auto-immersion settings ZENService is actually using.

    Taken from the last ``SetServiceOptions`` request in the ZENService log
    (sent by ZEN.exe at startup). Returns ``(options_dict, timestamp)``, or
    ``(None, None)`` if the entry has rotated out (logs are kept ~5 days) or
    lies beyond the scan bound.
    """
    found = _search_zen_logs("ZENService.log.xml", b"AutoImmersionRefillInterval",
                             _last_service_immersion_options)
    return found if found else (None, None)


# ---------------------------------------------------------------------------
# The carrier bottom (material, thickness) that DefiniteFocus uses.
#
# ZEN sends it to DF as `MtbBottomPropertiesChangedEvent(): material = Glass,
# thickness = 168000` (nm) in ZENService.log.xml, only when the value changes.
# On the dry objectives DF adds an offset computed from it (about 0.6 µm Z for
# each µm of thickness), and the material changes the offset calculation
# (DEV_NOTES "Dry objectives: DF finds the plate UNDERSIDE"). During a tray
# load ZEN sends thickness 0 for some seconds.
# ---------------------------------------------------------------------------

_DF_BOTTOM_RE = re.compile(
    r'timestamp="([^"]+)"[^>]*><message>[^<]*MtbBottomPropertiesChangedEvent\(\): '
    r'material = ([^,<]+), thickness = (-?\d+)'
)


def _last_df_bottom(text):
    """Pure parse: the last DF bottom event in ZENService log text, or None.

    Returns ``{"timestamp": str, "material": str, "thickness_um": float}``.
    """
    matches = _DF_BOTTOM_RE.findall(text)
    if not matches:
        return None
    timestamp, material, nm = matches[-1]
    return {"timestamp": timestamp, "material": material.strip(),
            "thickness_um": int(nm) / 1000.0}


def zen_df_bottom():
    """Best-effort: the carrier bottom (material, thickness) DF last received.

    Read from the newest ZENService log that contains the event. Returns the
    dict of ``_last_df_bottom``, or None if the event has rotated out or lies
    beyond the scan bound. Reads up to ~10 MB per log file: use it at run
    start and after a tray load, not before every DF call.
    """
    return _search_zen_logs("ZENService.log.xml", b"MtbBottomPropertiesChangedEvent",
                            _last_df_bottom)


def zen_auto_immersion_settings():
    """Best-effort snapshot of the CD7 auto-immersion settings, for logging.

    Returns a dict:
      * ``user_options_path``: Path of the logged-in user's Options.config, or None;
      * ``user_options``: the AUTO_IMMERSION_KEYS found in that file, or None;
      * ``service_options`` / ``service_options_time``: the same keys as ZEN sent
        them to ZENService, and when, or None.
    Never raises on a missing or unreadable source; that entry is just None.
    """
    info = {"user_options_path": None, "user_options": None,
            "service_options": None, "service_options_time": None}
    try:
        path = zen_user_options_path()
        info["user_options_path"] = path
        if path is not None:
            text = path.read_bytes().decode("utf-8-sig", errors="ignore")
            info["user_options"] = _immersion_options_from_options_config(text) or None
    except OSError:
        pass
    try:
        info["service_options"], info["service_options_time"] = zen_service_immersion_options()
    except OSError:
        pass
    return info


def _format_refill(opts):
    """Short summary of one options dict, e.g. 'every 120 min / 200 mm'."""
    if not opts:
        return "unknown"
    if opts.get("IsAutoImmersionEnabled") is False:
        return "auto-immersion OFF"
    if opts.get("IsAutoImmersionRefillGlobalEnabled") is False:
        return "refill OFF"
    interval = opts.get("AutoImmersionRefillInterval", "?")
    if opts.get("IsAutoImmersionRefillTravelDistanceEnabled") is False:
        distance = "distance off"
    else:
        distance = f"{opts.get('AutoImmersionRefillTravelDistance', '?')} mm"
    text = f"every {interval} min / {distance}"
    if opts.get("IsAutoImmersionRefillForExperimentEnabled") is False:
        text += " (not during experiments)"
    return text


def _zen_user_from_options_path(path):
    """'mike' from ``...\\ZEN\\Users\\mike\\<workspace...>\\Options.config``."""
    parts = PureWindowsPath(path).parts
    for i, part in enumerate(parts[:-1]):
        if part.lower() == "users" and i > 0 and parts[i - 1].lower() == "zen":
            return parts[i + 1]
    return "DefaultUser" if "DefaultUser" in parts else "?"


def format_auto_immersion_settings(info):
    """One log line for ``zen_auto_immersion_settings()``; flags a mismatch."""
    path = info.get("user_options_path")
    user = _zen_user_from_options_path(path) if path is not None else "?"
    ui_opts, svc_opts = info.get("user_options"), info.get("service_options")
    line = (f"Auto-immersion refill: ZEN user '{user}' options {_format_refill(ui_opts)}  |  "
            f"ZENService (sent {info.get('service_options_time') or '?'}) "
            f"{_format_refill(svc_opts)}")
    if ui_opts and svc_opts and any(ui_opts[k] != svc_opts[k]
                                    for k in ui_opts if k in svc_opts):
        line += "  |  MISMATCH: changed in the ZEN UI since ZEN started?"
    return line
