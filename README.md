# SmartMic

Smart-microscope automation built on top of the Zeiss ZEN gRPC API (ZEN-API).

This repository contains **only the custom (`MS_*`) automation code**. It depends
on two Zeiss-provided packages that ship with the ZEN-API examples and are **not**
vendored here:

- `zen_api`        — auto-generated gRPC stubs
- `zen_api_utils`  — Zeiss helper utilities

## Layout

The repo root holds the **reusable API wrapper** (the "library"). Individual
projects/examples that consume it live under `projects/`, one folder each.

### API wrapper (repo root)

| File | Role |
|------|------|
| `MS_CD7_API_LoA.py` | Synchronous wrappers around the ZEN gRPC API |
| `MS_zenapi_focus.py` | DefiniteFocus / FocusService (Z-drive) helpers |
| `MS_zenapi_swaf.py` | Software-autofocus experiment helpers |
| `MS_zenapi_objectivechanger.py` | Objective / optovar control |
| `MS_zenapi_stage_LM.py` | XY-stage control |
| `MS_zenapi_experiment_methods.py` | Experiment load / clone / run |
| `MS_Helper_function.py` | Logging, position loading, focus scoring |
| `zeiss_paths.py` | Path bootstrap — see below |

### Projects (`projects/`)

| Folder | Role |
|--------|------|
| `projects/smartmic_poc/MS_SmartMic_PoC.py` | Smart-microscope proof-of-concept pipeline (entry point) |

Each project script adds the repo root to `sys.path` (two levels up) and then
imports `zeiss_paths`, so it can use the wrapper modules and the Zeiss
`zen_api` packages regardless of the working directory it is launched from.
`config.ini` is resolved relative to each `MS_zenapi_*` module's own directory
(the repo root), so it stays at the root and is shared by all projects.

Exploratory, test, and scratch scripts live in `sandbox/`, which is **git-ignored**
(kept locally, never pushed). Those scripts add the repo root to `sys.path`
themselves so they can still import the core modules above.

## How the Zeiss dependency is resolved

`zeiss_paths.py` inserts the Zeiss `python_examples` folder onto `sys.path` at
import time, so `zen_api` / `zen_api_utils` resolve without copying them here.
Every module that needs them imports `zeiss_paths` first.

Default location:

    C:/Users/zeiss/Zeiss_OAD/OAD/ZEN-API/python_examples

If that folder moves, either edit `ZEISS_EXAMPLES` in `zeiss_paths.py` or set the
environment variable:

    SMARTMIC_ZEISS_EXAMPLES=<path-to-python_examples>

## Configuration (`config.ini`)

The ZEN-API connection settings live in `config.ini`, which is **git-ignored**
because it holds a machine-specific control-token (a secret). It is **not** in
the repo — you must create it before running anything.

Copy the template and fill in your own values:

    copy config.ini.example config.ini   # Windows
    # cp config.ini.example config.ini   # macOS/Linux

Then edit `config.ini`:

    [api]
    host = 127.0.0.1
    port = 5002
    cert_file = C:\ProgramData\Carl Zeiss\ZenApiGateway\Certificates\ZenApiPersonalSigningRootCA.pem
    control-token = <paste-your-ZEN-API-control-token-here>

The `control-token` is issued by the ZEN application (ZEN-API gateway). Never
commit `config.ini` — only `config.ini.example` is tracked.

## Running

Use the existing `smartmic` pixi environment:

    pixi run -e smartmic poc

or run the script directly:

    pixi run -e smartmic python projects/smartmic_poc/MS_SmartMic_PoC.py
