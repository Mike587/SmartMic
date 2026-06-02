# SmartMic

Smart-microscope automation built on top of the Zeiss ZEN gRPC API (ZEN-API).

This repository contains **only the custom (`MS_*`) automation code**. It depends
on two Zeiss-provided packages that ship with the ZEN-API examples and are **not**
vendored here:

- `zen_api`        — auto-generated gRPC stubs
- `zen_api_utils`  — Zeiss helper utilities

## Layout

| File | Role |
|------|------|
| `MS_SmartMic_PoC.py` | Main proof-of-concept pipeline (entry point) |
| `MS_CD7_API_LoA.py` | Synchronous wrappers around the ZEN gRPC API |
| `MS_zenapi_focus.py` | DefiniteFocus / FocusService (Z-drive) helpers |
| `MS_zenapi_swaf.py` | Software-autofocus experiment helpers |
| `MS_zenapi_objectivechanger.py` | Objective / optovar control |
| `MS_zenapi_stage_LM.py` | XY-stage control |
| `MS_zenapi_experiment_methods.py` | Experiment load / clone / run |
| `MS_Helper_function.py` | Logging, position loading, focus scoring |
| `MS_FocusSanityCheck.py` | Stand-alone focus-quality check |
| `MS_focus_exploration.py` | Focus exploration / diagnostics |
| `zeiss_paths.py` | Path bootstrap — see below |

## How the Zeiss dependency is resolved

`zeiss_paths.py` inserts the Zeiss `python_examples` folder onto `sys.path` at
import time, so `zen_api` / `zen_api_utils` resolve without copying them here.
Every module that needs them imports `zeiss_paths` first.

Default location:

    C:/Users/zeiss/Zeiss_OAD/OAD/ZEN-API/python_examples

If that folder moves, either edit `ZEISS_EXAMPLES` in `zeiss_paths.py` or set the
environment variable:

    SMARTMIC_ZEISS_EXAMPLES=<path-to-python_examples>

## Running

Use the existing `smartmic` pixi environment:

    pixi run -e smartmic python MS_SmartMic_PoC.py
