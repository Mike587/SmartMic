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

"""Read facts the ZEN-API does not give from ZEN's own log / options files.

Read-only, best-effort: a missing, rotated or unreadable source gives None.
No gateway calls, no setting changes. Files in ``zeiss_paths.ZEN_LOGGING_DIR``;
``zeiss_paths.<name>`` also resolves every name here.
"""

import os
import re
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Dict, List, Optional, Tuple, TypedDict, TypeVar, Union

import zeiss_paths

T = TypeVar("T")
Options = Dict[str, Any]


class DfBottom(TypedDict):
    """The carrier bottom DefiniteFocus last received (``zen_df_bottom``)."""
    timestamp: str
    material: str
    thickness_um: float


_SOFTWARE_VERSION_RE = re.compile(r'name="SoftwareVersion"\s+value="([^"]+)"')
_ZEN_LOG_TAIL_BYTES = 65536


def zen_app_version() -> Optional[str]:
    """Version of the running ZEN application (e.g. ``"3.13.109.08000"``), or None.

    Not available over the API (DEV_NOTES "ZEN version is NOT available over
    the API"). The last ``SoftwareVersion`` in the tail of ZEN.log.xml (not
    well-formed XML), so a restart / upgrade mid-file is seen.
    """
    try:
        text = _tail(zeiss_paths.ZEN_LOGGING_DIR / "ZEN.log.xml", _ZEN_LOG_TAIL_BYTES)
    except OSError:
        return None
    return _last_software_version(text)


def _tail(path: Path, n_bytes: int) -> str:
    """Return the last ``n_bytes`` bytes of ``path``, decoded as UTF-8 (lossy)."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - n_bytes))
        data = f.read()
    return data.decode("utf-8", errors="ignore")


def _last_software_version(text: str) -> Optional[str]:
    """Pure text parse: the last ``SoftwareVersion="..."`` attribute in ``text``."""
    matches = _SOFTWARE_VERSION_RE.findall(text)
    return matches[-1] if matches else None


# ---------------------------------------------------------------------------
# Auto-immersion settings, read-only (not in the API: DEV_NOTES "The
# re-immersion settings are NOT reachable through the ZEN-API"). Two sources
# that can differ: the user's Options.config (the UI), and the last
# SetServiceOptions in the ZENService log (what the refiller uses).
# ---------------------------------------------------------------------------

# SetServiceOptions keys, in log order. Interval in min, distance in mm, durations in ms.
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

# Max log files (~10 MB each) scanned, newest first; past it a reader returns None.
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


def _immersion_options_from_options_config(text: str) -> Options:
    """Pure parse: the AUTO_IMMERSION_KEYS found in Options.config text (may be empty)."""
    found = {name: _coerce_option_value(value)
             for name, value in _OPTION_ENTRY_RE.findall(text)}
    return {k: found[k] for k in AUTO_IMMERSION_KEYS if k in found}


def _user_options_path_from_zen_log(text: str) -> Optional[str]:
    """Pure parse: the last per-USER Options.config path in ZEN.log.xml text, or None.

    Skips the workgroup file and the installation default (Program Files).
    """
    paths = [p for p in _OPTIONS_PATH_RE.findall(text)
             if ("\\Users\\" in p or "\\DefaultUser\\" in p)
             and "Program Files" not in p]
    return paths[-1] if paths else None


def _last_service_immersion_options(text: str) -> Tuple[Optional[Options], Optional[str]]:
    """Pure parse: the last SetServiceOptions payload with immersion keys.

    Returns ``(options_dict, timestamp)`` or ``(None, None)``.
    """
    for timestamp, body in reversed(_SERVICE_OPTIONS_RE.findall(text)):
        entries = dict(_PAYLOAD_ENTRY_RE.findall(body))
        opts = {k: _coerce_option_value(entries[k])
                for k in AUTO_IMMERSION_KEYS if k in entries}
        if opts:
            return opts, timestamp
    return None, None


def _zen_logs_newest_first(stem: str) -> List[Path]:
    """``ZEN_LOGGING_DIR/<stem>`` and its rotations (``<stem>.N``), newest first."""
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
    """Newest-first: ``parse(text)`` of the first log file containing ``marker``, or None."""
    for path in _zen_logs_newest_first(stem):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if marker in data:
            return parse(data.decode("utf-8", errors="ignore"))
    return None


def zen_user_options_path() -> Optional[Path]:
    """Path of the logged-in ZEN user's Options.config (from ZEN.log.xml), or None."""
    found = _search_zen_logs("ZEN.log.xml", b"Options.config",
                             _user_options_path_from_zen_log)
    return Path(found) if found else None


def zen_service_immersion_options() -> Tuple[Optional[Options], Optional[str]]:
    """Auto-immersion settings ZENService uses (last ``SetServiceOptions``).

    Returns ``(options_dict, timestamp)``, or ``(None, None)`` if rotated out
    or beyond the scan bound.
    """
    found = _search_zen_logs("ZENService.log.xml", b"AutoImmersionRefillInterval",
                             _last_service_immersion_options)
    return found if found else (None, None)


# ---------------------------------------------------------------------------
# The carrier bottom DefiniteFocus uses: `MtbBottomPropertiesChangedEvent()`
# in ZENService.log.xml, thickness in nm, logged only on change (DEV_NOTES "Dry
# objectives: DF finds the plate UNDERSIDE").
# ---------------------------------------------------------------------------

_DF_BOTTOM_RE = re.compile(
    r'timestamp="([^"]+)"[^>]*><message>[^<]*MtbBottomPropertiesChangedEvent\(\): '
    r'material = ([^,<]+), thickness = (-?\d+)'
)


def _last_df_bottom(text: str) -> Optional[DfBottom]:
    """Pure parse: the last DF bottom event in ZENService log text, or None."""
    matches = _DF_BOTTOM_RE.findall(text)
    if not matches:
        return None
    timestamp, material, nm = matches[-1]
    return {"timestamp": timestamp, "material": material.strip(),
            "thickness_um": int(nm) / 1000.0}


def zen_df_bottom() -> Optional[DfBottom]:
    """The carrier bottom (material, thickness_um) DF last received, or None.

    Reads up to ~10 MB per log file: call at run start / after a tray load,
    not before every DF call.
    """
    return _search_zen_logs("ZENService.log.xml", b"MtbBottomPropertiesChangedEvent",
                            _last_df_bottom)


def zen_auto_immersion_settings() -> Dict[str, Any]:
    """Snapshot of the auto-immersion settings, for logging. Never raises on a bad source.

    Keys (None if unknown): ``user_options_path``, ``user_options``,
    ``service_options``, ``service_options_time``.
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


def _format_refill(opts: Optional[Options]) -> str:
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


def _zen_user_from_options_path(path: Union[str, Path]) -> str:
    """'mike' from ``...\\ZEN\\Users\\mike\\<workspace...>\\Options.config``."""
    parts = PureWindowsPath(path).parts
    for i, part in enumerate(parts[:-1]):
        if part.lower() == "users" and i > 0 and parts[i - 1].lower() == "zen":
            return parts[i + 1]
    return "DefaultUser" if "DefaultUser" in parts else "?"


def format_auto_immersion_settings(info: Dict[str, Any]) -> str:
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
