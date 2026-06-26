# SmartMic

Smart-microscope automation built on top of the Zeiss ZEN gRPC API (ZEN-API).

This repository contains the custom (`MS_*`) automation code. It depends on the
Zeiss-provided **`zen_api`** package (auto-generated gRPC stubs), which ships with
the ZEN-API examples and is **not** vendored here — see
[How the Zeiss dependency is resolved](#how-the-zeiss-dependency-is-resolved).
The small set of ZEN-API helpers SmartMic needs (channel init, logging,
objective/optovar lookups) are vendored in `MS_zenapi_helpers.py`.

## Layout

The repo root holds the **reusable API wrapper** (the "library"). Example
pipelines that consume it live under `projects/`, one folder each.

### API wrapper (repo root)

| File | Role |
|------|------|
| `MS_CD7_API_LoA.py` | Synchronous wrappers around the ZEN gRPC API |
| `MS_zenapi_focus.py` | DefiniteFocus / FocusService (Z-drive) helpers |
| `MS_zenapi_swaf.py` | Software-autofocus experiment helpers |
| `MS_zenapi_objectivechanger.py` | Objective / optovar control |
| `MS_zenapi_stage_LM.py` | XY-stage control |
| `MS_zenapi_sample_carrier.py` | Sample-carrier (well-plate) info query |
| `MS_zenapi_experiment_methods.py` | Experiment load / clone / run / status; run by name, path, or XML |
| `MS_czexp_editor.py` | Read/modify ZEN `.czexp` files (position, z-stack, scan crop) |
| `MS_Helper_function.py` | Logging, position loading, focus scoring |
| `MS_zenapi_helpers.py` | Vendored ZEN-API glue (channel init, logging, objective lookups) |
| `MS_image_analysis.py` | Launcher for external image-analysis scripts (own pixi env) |
| `zeiss_paths.py` | Path bootstrap — see below |

### Projects (`projects/`)

| Folder | Role |
|--------|------|
| `projects/smartmic_poc/MS_SmartMic_PoC.py` | Smart-microscope proof-of-concept pipeline (entry point) |

Each project script adds the repo root to `sys.path` (two levels up) and then
imports `zeiss_paths`, so it can use the wrapper modules and the Zeiss `zen_api`
package regardless of the working directory it is launched from. `config.ini` is
resolved relative to the repo root, so it stays at the root and is shared by all
projects.

## Image analysis

Smart microscopy pairs acquisition with image analysis. The analysis half is kept
in **separate, self-contained repos**, each with its own pixi environment, so its
dependencies never conflict with the ZEN-API environment. `MS_image_analysis.py`
launches such a project as a subprocess: it passes a CZI, an output directory and
a `--prefix`, and reads back a `<prefix>_targets.json` listing the detected
targets (each with absolute stage coordinates).

The reference analysis used by `smartmic_poc` is the nuclei-detection scaffold at
**[Mike587/ia_PoC_002](https://github.com/Mike587/ia_PoC_002)**, which also
documents the input/output contract for building new analyses.

## How the Zeiss dependency is resolved

`zeiss_paths.py` inserts the Zeiss `python_examples` folder (and the ZEN-API
package `src`) onto `sys.path` at import time, so `zen_api` resolves without
copying it here. Every module that needs it imports `zeiss_paths` first.

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

## Testing

There are two tiers of tests: fast **offline** unit tests (no microscope) and
**hardware** tests that run against the live ZEN-API gateway with the 384-well
plate loaded. Hardware tests are skipped unless you pass `--run-hardware`, so a
plain run never moves the scope:

    pixi run -e smartmic test        # offline unit tests only
    pixi run -e smartmic test-hw     # hardware tests (on the scope, opt-in)

See [`tests/README.md`](tests/README.md) for the full step-by-step guide and
[`tests/TEST_PLAN.md`](tests/TEST_PLAN.md) for the design.
