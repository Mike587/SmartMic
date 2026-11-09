"""
zeiss_paths.py

Standalone SmartMic project bootstrap.

The MS_* modules depend on the Zeiss-provided ``zen_api`` package (the
auto-generated gRPC stubs), which lives in the Zeiss ZEN-API example/package
tree, not in this repository.  Importing this module inserts that tree (and this
project's own directory) onto ``sys.path`` so ``import zen_api`` resolves no
matter where a script is launched from.

(The ``zen_api_utils`` example glue is no longer a dependency — the helpers
SmartMic used are vendored in ``MS_zenapi_helpers.py``.)

Usage — import this FIRST, before any MS_* / zen_api import::

    import zeiss_paths  # noqa: F401  (side effect: extends sys.path)

If the Zeiss example folder ever moves, update ZEISS_EXAMPLES below (or set the
SMARTMIC_ZEISS_EXAMPLES environment variable to override it).
"""

import os
import re
import subprocess
import sys
from pathlib import Path, PureWindowsPath

# Location of the Zeiss ZEN-API python_examples folder (used to locate zen_api;
# in the OLD layout zen_api shipped loose here).  Override with the
# SMARTMIC_ZEISS_EXAMPLES env var if needed.
ZEISS_EXAMPLES = Path(
    os.environ.get(
        "SMARTMIC_ZEISS_EXAMPLES",
        r"C:/Users/zeiss/Zeiss_OAD/OAD/ZEN-API/python_examples",
    )
)

# This project's own directory, so the MS_* modules can import each other.
THIS_DIR = Path(__file__).resolve().parent

# The ZEN-API connection config lives at the repo root next to this file.
# Single source of truth: every MS_zenapi_* module imports CONFIG_PATH from here
# instead of recomputing ``Path(__file__).parent / "config.ini"`` itself.  It is
# absolute, so it does not depend on the current working directory.
CONFIG_PATH = THIS_DIR / "config.ini"

# Where ZEN and the ZEN-API gateway write their own logs / binaries. Used only
# to recover version info the gRPC API itself doesn't expose (see
# zen_app_version / zen_api_gateway_version below). Override via env vars if a
# machine's install differs from the standard layout.
ZEN_LOGGING_DIR = Path(
    os.environ.get("SMARTMIC_ZEN_LOGGING_DIR", r"C:/ProgramData/Carl Zeiss/Logging")
)
ZEN_API_GATEWAY_EXE = Path(
    os.environ.get(
        "SMARTMIC_ZEN_API_GATEWAY_EXE",
        r"C:/Program Files/Carl Zeiss/ZenApiGateway/ZenApiGateway.exe",
    )
)

# IMPORTANT ordering:
#   * THIS_DIR goes to the FRONT  -> this project's MS_* modules win.
#   * ZEISS_EXAMPLES goes to the BACK -> used only as a fallback for
#     zen_api (which does not exist in this project).
# Otherwise the old MS_* copies still sitting in the Zeiss folder would
# shadow the ones in this repository.
_this = str(THIS_DIR)
if _this in sys.path:
    sys.path.remove(_this)
sys.path.insert(0, _this)

# In the OLD ZEN-API layout, python_examples also held a loose zen_api folder,
# so keep it on the path as a fallback for that layout.
_zeiss = str(ZEISS_EXAMPLES)
if _zeiss not in sys.path:
    sys.path.append(_zeiss)

def _zen_api_src_version_key(src_path):
    """Numeric sort key for a ``.../zen_api-<version>/src`` path.

    Sorting the raw path strings is lexical, so e.g. ``zen_api-2025.10.0``
    would sort BEFORE ``zen_api-2025.9.0`` once a version component reaches two
    digits ('1' < '9') — silently picking the older package as "newest". Parse
    each dot-separated component as an int instead. Falls back to ``(-1,)``
    (sorts before any real version) for a folder name that doesn't parse, so a
    malformed entry never wins "newest" instead of crashing the comparison.
    """
    version = src_path.parent.name[len("zen_api-"):]
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (-1,)


# NEW ZEN-API layout (>= 2026.05): zen_api is a separate installable package at
# ZEN-API/python_package/zen_api-<version>/src/.  Add the newest one's src so
# `import zen_api` resolves without pip-installing it.  (The OLD layout had
# zen_api loose in python_examples, already covered above.)
_zen_api_srcs = sorted(
    (ZEISS_EXAMPLES.parent / "python_package").glob("zen_api-*/src"),
    key=_zen_api_src_version_key,
)
# The repo can ship MULTIPLE package versions, and the newest may be ahead of
# the gateway.  Pin one with SMARTMIC_ZEN_API_VERSION
# (e.g. "2025.10.1"); otherwise fall back to the newest available.
_pin = os.environ.get("SMARTMIC_ZEN_API_VERSION")
if _pin:
    _pinned = [s for s in _zen_api_srcs if s.parent.name == f"zen_api-{_pin}"]
    _zen_api_srcs = _pinned or _zen_api_srcs
if _zen_api_srcs:
    _src = str(_zen_api_srcs[-1])  # numeric version sort (see _zen_api_src_version_key); newest is last
    if _src not in sys.path:
        sys.path.append(_src)

# Warn only if zen_api isn't resolvable from ANY location we added.
_zen_api_locations = [ZEISS_EXAMPLES, *_zen_api_srcs]
if not any((Path(p) / "zen_api").is_dir() for p in _zen_api_locations):
    sys.stderr.write(
        f"[zeiss_paths] WARNING: zen_api not found under {ZEISS_EXAMPLES} or "
        f"{ZEISS_EXAMPLES.parent / 'python_package'}/zen_api-*/src. "
        f"Set SMARTMIC_ZEISS_EXAMPLES to the correct python_examples path.\n"
    )


def zen_api_version():
    """Best-effort identity of the resolved ``zen_api`` gRPC stubs, for logging.

    The ZEN gRPC API exposes NO version/about service, and the ``zen_api`` package
    has no ``__version__`` / pip metadata (see DEV_NOTES). The only version signal
    is the package folder name in the NEW layout
    (``…/python_package/zen_api-<version>/src/zen_api``); the OLD loose layout
    (``python_examples/zen_api``) carries no version.

    Returns ``(version, path)`` where ``version`` is the string parsed from a
    ``zen_api-<version>`` ancestor folder or ``None`` if not present, and ``path``
    is the resolved ``zen_api`` package directory (or ``None`` if unimportable).
    Always log ``path`` too — on a machine with both layouts the loose copy can
    shadow a versioned package, so the version alone can be misleading.
    """
    try:
        import zen_api
        pkg_dir = Path(zen_api.__file__).resolve().parent
    except Exception:
        return None, None
    return _zen_api_version_from_dir(pkg_dir), str(pkg_dir)


def _zen_api_version_from_dir(pkg_dir):
    """Parse the version from a ``zen_api-<version>`` ancestor of a package dir.

    Returns the version string (e.g. ``"2025.10.1"``) or ``None`` if no such
    ancestor exists (the OLD loose layout). Pure/​path-only — no I/O — so it is
    unit-testable against synthetic paths for both layouts.
    """
    for parent in Path(pkg_dir).parents:
        if parent.name.startswith("zen_api-"):
            return parent.name[len("zen_api-"):]
    return None


_SOFTWARE_VERSION_RE = re.compile(r'name="SoftwareVersion"\s+value="([^"]+)"')
_ZEN_LOG_TAIL_BYTES = 65536


def zen_app_version():
    """Best-effort version of the running ZEN APPLICATION itself, for logging.

    Confirmed unavailable over the gRPC API (see ``zen_api_version`` above): none
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
        text = _tail(ZEN_LOGGING_DIR / "ZEN.log.xml", _ZEN_LOG_TAIL_BYTES)
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


def zen_api_gateway_version():
    """Best-effort FileVersion of ``ZenApiGateway.exe``, for logging.

    This is the actual gRPC service SmartMic connects to — its version tracks
    the API *contract* (message shapes, RPC behavior) more directly than either
    the ZEN application version (``zen_app_version``) or the ``zen_api`` Python
    stub version (``zen_api_version``), since the gateway can be serviced
    somewhat independently of the rest of ZEN. Not available over the API
    itself (no version/about call); read via the Win32 file-version resource
    instead (``win32api``, already present transitively in this env).

    Returns the version string (e.g. ``"3.6.25262.3"``), or ``None`` if the exe
    is missing, ``win32api`` isn't importable, or its version resource can't be
    read.
    """
    try:
        import win32api
    except ImportError:
        return None
    try:
        info = win32api.GetFileVersionInfo(str(ZEN_API_GATEWAY_EXE), "\\")
        ms, ls = info["FileVersionMS"], info["FileVersionLS"]
    except Exception:
        return None
    return f"{win32api.HIWORD(ms)}.{win32api.LOWORD(ms)}.{win32api.HIWORD(ls)}.{win32api.LOWORD(ls)}"


def smartmic_version():
    """Best-effort identity of the exact SmartMic code that is running, for logging.

    There is no formal release process yet (no ``__version__``, no git tags —
    ``pixi.toml``'s ``version = "0.1.0"`` is a static placeholder that has never
    been bumped), so this reads it straight from git instead:
    ``git describe --tags --always --dirty``, run against THIS_DIR (SmartMic's
    own repo root) rather than the caller's cwd, so it reports SmartMic's commit
    regardless of which project imports it or where that project's own repo is.

    Falls back gracefully at every stage: no tags yet -> just the abbreviated
    commit hash (e.g. ``"3bf63cf"``); uncommitted changes on top of it ->
    ``-dirty`` appended (e.g. ``"3bf63cf-dirty"``); once ``PROJECT_CHECKLIST.md``'s
    freezing workflow actually tags a release, this starts returning a real
    version automatically (e.g. ``"v0.2.0-5-g3bf63cf-dirty"``) with no code change
    needed here.

    Returns the describe string, or ``None`` if git itself is unavailable or
    THIS_DIR isn't inside a git repo (e.g. a project that vendored/copied the
    SmartMic modules per the freezing workflow, rather than importing them live).
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(THIS_DIR), "describe", "--tags", "--always", "--dirty"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_state(repo):
    """Exact code state of the git checkout containing ``repo``, for run records.

    Unlike :func:`smartmic_version` (a short ``describe`` string for log lines),
    this gives the FULL commit hash plus the list of uncommitted files, so a
    dataset can be traced back to the exact code that produced it. Works for any
    repo: SmartMic itself, a project with its own git repo, an analysis repo.

    Returns ``{"root": str | None, "commit": str | None, "dirty_files": [str]}``:

    * ``root`` — top level of the checkout that git actually reported on. It can
      differ from ``repo``: a folder that is NOT its own repo but sits inside one
      (e.g. a project folder without its own ``.git`` under ``SmartMic/projects``)
      reports the ENCLOSING repo — ``root`` makes that visible.
    * ``commit`` — ``None`` if git is unavailable, ``repo`` doesn't exist or is not
      in a git checkout (e.g. a vendored/frozen copy), or the repo has no commit.
    * ``dirty_files`` — ``git status --porcelain`` paths (relative to ``root``),
      tracked and untracked; ``[]`` when clean or unknown.
    """
    def git(*args):
        try:
            r = subprocess.run(["git", "-C", str(repo), *args],
                               capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout if r.returncode == 0 else None

    root = git("rev-parse", "--show-toplevel")
    commit = git("rev-parse", "HEAD") if root else None
    status = (git("status", "--porcelain") or "") if root else ""
    return {"root": str(Path(root.strip())) if root else None,
            "commit": commit.strip() if commit else None,
            "dirty_files": [line[3:] for line in status.splitlines() if line.strip()]}


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
        files = [f for f in ZEN_LOGGING_DIR.glob(f"{stem}*")
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
