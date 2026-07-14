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
from pathlib import Path

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

# NEW ZEN-API layout (>= 2026.05): zen_api is a separate installable package at
# ZEN-API/python_package/zen_api-<version>/src/.  Add the newest one's src so
# `import zen_api` resolves without pip-installing it.  (The OLD layout had
# zen_api loose in python_examples, already covered above.)
_zen_api_srcs = sorted((ZEISS_EXAMPLES.parent / "python_package").glob("zen_api-*/src"))
# The repo can ship MULTIPLE package versions, and the newest may be ahead of
# the gateway.  Pin one with SMARTMIC_ZEN_API_VERSION
# (e.g. "2025.10.1"); otherwise fall back to the newest available.
_pin = os.environ.get("SMARTMIC_ZEN_API_VERSION")
if _pin:
    _pinned = [s for s in _zen_api_srcs if s.parent.name == f"zen_api-{_pin}"]
    _zen_api_srcs = _pinned or _zen_api_srcs
if _zen_api_srcs:
    _src = str(_zen_api_srcs[-1])  # version dirs sort lexically; newest is last
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
