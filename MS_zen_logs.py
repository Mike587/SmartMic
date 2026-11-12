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

"""Read the data that the ZEN-API does not give, from the files of ZEN.

The sources are the log files and the options files of ZEN. This module is
read-only, and it tries its best. If a source is missing, if ZEN rotated it, or
if the module cannot read it, the function gives ``None``. The module makes no
gateway call and changes no setting. The files are in
``zeiss_paths.ZEN_LOGGING_DIR``. ``zeiss_paths.<name>`` also finds each name in
this module.
"""

import contextlib
import os
import re
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Dict, List, Optional, Tuple, TypedDict, TypeVar, Union

import zeiss_paths

T = TypeVar("T")
Options = Dict[str, Any]


class DfBottom(TypedDict):
    """The carrier bottom that DefiniteFocus received most recently (``zen_df_bottom``)."""
    timestamp: str
    material: str
    thickness_um: float


_SOFTWARE_VERSION_RE = re.compile(r'name="SoftwareVersion"\s+value="([^"]+)"')
_ZEN_LOG_TAIL_BYTES = 65536


def zen_app_version() -> Optional[str]:
    """Get the version of the ZEN application that runs, or ``None``.

    An example of a version is ``"3.13.109.08000"``. The API does not give this
    value (DEV_NOTES "ZEN version is NOT available over the API"). The function
    reads the last ``SoftwareVersion`` at the end of ZEN.log.xml. That file is
    not well-formed XML. Thus the function also sees a restart or an upgrade in
    the middle of the file.
    """
    try:
        text = _tail(zeiss_paths.ZEN_LOGGING_DIR / "ZEN.log.xml", _ZEN_LOG_TAIL_BYTES)
    except OSError:
        return None
    return _last_software_version(text)


def _tail(path: Path, n_bytes: int) -> str:
    """Get the last ``n_bytes`` bytes of ``path`` as UTF-8 text.

    The function ignores each byte that it cannot decode.
    """
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - n_bytes))
        data = f.read()
    return data.decode("utf-8", errors="ignore")


def _last_software_version(text: str) -> Optional[str]:
    """Find the last ``SoftwareVersion="..."`` attribute in ``text``.

    The function reads only text. It does no file operation.
    """
    matches = _SOFTWARE_VERSION_RE.findall(text)
    return matches[-1] if matches else None


# ---------------------------------------------------------------------------
# The auto-immersion settings. These functions are read-only. The API does not
# give these settings (DEV_NOTES "The re-immersion settings are NOT reachable
# through the ZEN-API"). There are two sources, and they can be different: the
# Options.config of the user, which is the user interface, and the last
# SetServiceOptions in the ZENService log, which the refiller uses.
# ---------------------------------------------------------------------------

# The SetServiceOptions keys, in the order of the log. The interval is in
# minutes, the distance is in mm, and the durations are in ms.
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

# The maximum number of log files that a reader examines, newest first. Each
# file is approximately 10 MB. After this count, a reader gives None.
_ZEN_LOG_MAX_FILES = 40

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


def _coerce_option_value(text: str) -> Any:
    """Change an option text into a Python value.

    ``"true"`` and ``"True"`` become a bool. ``"120"`` becomes an int.
    ``"0.5"`` becomes a float. Each other text stays a string.
    """
    low = text.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    return text


def _immersion_options_from_options_config(text: str) -> Options:
    """Find the AUTO_IMMERSION_KEYS in Options.config text.

    The function reads only text. The result can be empty.
    """
    found = {name: _coerce_option_value(value)
             for name, value in _OPTION_ENTRY_RE.findall(text)}
    return {k: found[k] for k in AUTO_IMMERSION_KEYS if k in found}


def _user_options_path_from_zen_log(text: str) -> Optional[str]:
    """Find the last Options.config path of a USER in ZEN.log.xml text.

    The function reads only text. It ignores the workgroup file and the
    default file of the installation, which is in Program Files. It gives
    ``None`` if it finds no path.
    """
    paths = [p for p in _OPTIONS_PATH_RE.findall(text)
             if ("\\Users\\" in p or "\\DefaultUser\\" in p)
             and "Program Files" not in p]
    return paths[-1] if paths else None


def _last_service_immersion_options(text: str) -> Tuple[Optional[Options], Optional[str]]:
    """Find the last SetServiceOptions data that holds immersion keys.

    The function reads only text.

    Returns:
        ``(options_dict, timestamp)``, or ``(None, None)``.
    """
    for timestamp, body in reversed(_SERVICE_OPTIONS_RE.findall(text)):
        entries = dict(_PAYLOAD_ENTRY_RE.findall(body))
        opts = {k: _coerce_option_value(entries[k])
                for k in AUTO_IMMERSION_KEYS if k in entries}
        if opts:
            return opts, timestamp
    return None, None


def _zen_logs_newest_first(stem: str) -> List[Path]:
    """Get ``ZEN_LOGGING_DIR/<stem>`` and the files that ZEN rotated from it.

    The names of the rotated files are ``<stem>.N``. The newest file is first.
    """
    try:
        files = [f for f in zeiss_paths.ZEN_LOGGING_DIR.glob(f"{stem}*")
                 if f.name == stem or f.name[len(stem):].lstrip(".").isdigit()]
    except OSError:
        return []

    def mtime(f: Path) -> float:
        try:
            return f.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(files, key=mtime, reverse=True)[:_ZEN_LOG_MAX_FILES]


def _search_zen_logs(stem: str, marker: bytes, parse: Callable[[str], T]) -> Optional[T]:
    """Read the log files, newest first, and parse the first file that holds ``marker``.

    Returns:
        The result of ``parse(text)``, or ``None`` if no file holds the marker.
    """
    for path in _zen_logs_newest_first(stem):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if marker in data:
            return parse(data.decode("utf-8", errors="ignore"))
    return None


def zen_user_options_path() -> Optional[Path]:
    """Get the Options.config path of the ZEN user who is signed in, or ``None``.

    The path comes from ZEN.log.xml.
    """
    found = _search_zen_logs("ZEN.log.xml", b"Options.config",
                             _user_options_path_from_zen_log)
    return Path(found) if found else None


def zen_service_immersion_options() -> Tuple[Optional[Options], Optional[str]]:
    """Get the auto-immersion settings that ZENService uses.

    The values come from the last ``SetServiceOptions``.

    Returns:
        ``(options_dict, timestamp)``. The result is ``(None, None)`` if ZEN
        rotated the data out, or if the data is in a file beyond the maximum
        count.
    """
    found = _search_zen_logs("ZENService.log.xml", b"AutoImmersionRefillInterval",
                             _last_service_immersion_options)
    return found if found else (None, None)


# ---------------------------------------------------------------------------
# The carrier bottom that DefiniteFocus uses. The source is the
# `MtbBottomPropertiesChangedEvent()` entry in ZENService.log.xml. The
# thickness is in nm. ZEN writes this entry only when the value changes
# (DEV_NOTES "Dry objectives: DF finds the plate UNDERSIDE").
# ---------------------------------------------------------------------------

_DF_BOTTOM_RE = re.compile(
    r'timestamp="([^"]+)"[^>]*><message>[^<]*MtbBottomPropertiesChangedEvent\(\): '
    r'material = ([^,<]+), thickness = (-?\d+)'
)


def _last_df_bottom(text: str) -> Optional[DfBottom]:
    """Find the last DF bottom event in ZENService log text, or give ``None``.

    The function reads only text.
    """
    matches = _DF_BOTTOM_RE.findall(text)
    if not matches:
        return None
    timestamp, material, nm = matches[-1]
    return {"timestamp": timestamp, "material": material.strip(),
            "thickness_um": int(nm) / 1000.0}


def zen_df_bottom() -> Optional[DfBottom]:
    """Get the carrier bottom that DF received most recently, or ``None``.

    The result holds the material and ``thickness_um``. The function reads as
    much as 10 MB for each log file. Thus call it at the start of a run, or
    after a tray load. Do not call it before each DF call.
    """
    return _search_zen_logs("ZENService.log.xml", b"MtbBottomPropertiesChangedEvent",
                            _last_df_bottom)


def zen_auto_immersion_settings() -> Dict[str, Any]:
    """Get the auto-immersion settings at this moment, for the log.

    A source that the function cannot read raises no exception.

    Returns:
        A dict with these keys: ``user_options_path``, ``user_options``,
        ``service_options`` and ``service_options_time``. A value is ``None``
        if the function cannot find it.
    """
    info = {"user_options_path": None, "user_options": None,
            "service_options": None, "service_options_time": None}
    with contextlib.suppress(OSError):
        path = zen_user_options_path()
        info["user_options_path"] = path
        if path is not None:
            text = path.read_bytes().decode("utf-8-sig", errors="ignore")
            info["user_options"] = _immersion_options_from_options_config(text) or None
    with contextlib.suppress(OSError):
        info["service_options"], info["service_options_time"] = zen_service_immersion_options()
    return info


def _format_refill(opts: Optional[Options]) -> str:
    """Make a short summary of one options dict.

    An example of the result is "every 120 min / 200 mm".
    """
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


def _zen_user_from_options_path(path: Union[str, Path]) -> str:
    """Get the ZEN user name from an Options.config path.

    The path ``...\\ZEN\\Users\\mike\\<workspace...>\\Options.config`` gives
    ``"mike"``.
    """
    parts = PureWindowsPath(path).parts
    for i, part in enumerate(parts[:-1]):
        if part.lower() == "users" and i > 0 and parts[i - 1].lower() == "zen":
            return parts[i + 1]
    return "DefaultUser" if "DefaultUser" in parts else "?"


def format_auto_immersion_settings(info: Dict[str, Any]) -> str:
    """Make one log line from ``zen_auto_immersion_settings()``.

    The line holds a MISMATCH note if the two sources are different.
    """
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
