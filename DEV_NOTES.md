# SmartMic — dev notes (learnings + decisions)

Things we learned and things we decided that should persist — the kind of
non-obvious knowledge that isn't visible from reading a single file. Maintainer
notes that don't belong in the public README.

Open tasks live in `TODO.md`; licensing/attribution decisions live in
`copyright.md`. This file is everything else worth remembering.

**Fuller docs:**
- How to run the tests: [`tests/README.md`](tests/README.md)
- Test design + on-scope findings: [`tests/TEST_PLAN.md`](tests/TEST_PLAN.md) (esp. §12)
- Architecture & layout: [`README.md`](README.md)
- Function-by-function map: `overview.md` (*git-ignored — regenerate as needed*)

---

## Hardware / ZEN gotchas (learned on the CD7, June 2026)

- **This CD7 is inverted; Z=0 breaks the immersion bridge (on the 50x objective).** Lowering the Z-drive
  to 0 fully retracts the objective away from the slide, so the immersion water
  bridge falls. Anything that drops Z to 0 (notably
  `MS_zenapi_stage_LM.move_stage_to_new_xy_position`, which does so for collision
  safety before an XY move) **must not be used with the 50x immersion objective** —
  it kills immersion. Travel XY at a Z inside the immersion, or let the ZEN
  experiment do the move (its immersion logic raises/lowers Z safely).

  **Decided mitigation (2026-06-30): guard, don't reimplement.** We do NOT build
  an "immersion-safe" direct move that travels at a non-zero Z — choosing a travel
  Z that clears well walls / slide edges *and* keeps the immersion bridge is exactly
  the delicate geometry ZEN already does safely inside an experiment, and getting it
  wrong risks a crash. Instead, `move_stage_to_new_xy_position` **raises** when the
  immersion objective (changer position `IMMERSION_OBJECTIVE_POSITION = 4`, the only
  immersion objective on this scope) is active, so the caller is forced down the
  known-good path: write the target XY(Z) into a `.czexp` and let ZEN drive the
  stage (move → image → move back, immersion-safe). It is an error, not a warning —
  a warning that's ignored still drops Z to 0 and kills immersion.

- **DefiniteFocus start position.** DF FindSurface is designed to locate the surface
  from *any* start, so the −300 µm default is conceptually fine — but on this Z-drive
  −300 µm is **below the reachable minimum** (the drive floors near −293 µm), and ZEN
  raises `INTERNAL: Requested position was not reached`. The initial start-move in
  `definite_focus_find_surface` is wrapped so a near-miss is tolerated and the search
  proceeds. **Surface Z is plate-dependent** (strongly affected by the carrier's
  *skirt* parameter) — never hard-code or assume a surface Z.

- **DefiniteFocus FindSurface capture range is objective-dependent.** At 5x/0.35
  (dry, long working distance) FindSurface locates the surface from Z=0 (it sweeps
  the full range). At 50x/1.2 **immersion** the same call from Z=0 (or 50/100 µm)
  **fails with `INTERNAL: 13`** — it cannot sweep the ~1.2 mm to the surface and
  needs to start within a few hundred µm of it. (Compounded by the Z=0 immersion
  loss above.) So "DF works from any Z" holds for the dry low-mag objective but NOT
  for the 50x immersion one — seed it near the surface, or focus inside the
  experiment.

- **5x↔50x surfaces differ a lot here.** The 5x tissue-scan surface read ~1189–1194
  µm; a 50x DF (seeded at 1080 µm) landed ~939 µm. Treat the per-objective surface
  Z as independent — never assume 5x and 50x focus at the same Z.

- **ZEN computes the tile grid from the CURRENT objective/optovar at RUN time.**
  Launching an experiment before a slow optics change has propagated (especially
  moving OFF the immersion objective) makes ZEN tile for the stale config → wrong
  per-tile FOV/overlap (a doubled, ghosted mosaic). Verify the read-back AND let it
  settle before acquiring (HD_Nuclei's `ensure_optics`). See TODO for a possible
  ZEN-native "defined config" fix being investigated.

- **A water drop is left behind after using the immersion objective.** Once the
  immersion (50x) objective has been at a position, a small drop of water remains
  there on the sample. Switching back to a non-immersion (dry) objective is fine,
  but it must happen **'far away' from the immersion position — several mm** — so the
  dry objective doesn't dip into the leftover drop. On a 96-well plate, moving to the
  next well is roughly enough separation. So plan objective-back-switches (or the
  next dry acquisition) away from where immersion was used.

- **50x DefiniteFocus ERRORS at some sites, with no in-place retry.** On the
  inverted CD7 the internal DF (FindSurface) used by HD_Nuclei's find_thickness
  intermittently fails to lock at a site (ZEN throws a DF error). There is no way
  to redo the DF from code, and the stack then comes back out-of-focus and **dark**.
  The only viable handling is to **detect the empty stack and skip the site** (do
  NOT proceed to find_nuclei / HQ on it). Detector used: the **99.9th-percentile
  intensity / detector full-scale** — robust to hot pixels (a dud stack can still
  carry a stray bright pixel, so raw `max` is misleading). Measured on the slide:
  dud ≈ 0.008–0.012, focused tissue ≈ 0.10–0.14 → threshold **0.04**. Implemented
  both analysis-side in `ia_NfS` (`measure_thickness` emits `low_signal`;
  `find_nuclei_bboxes` bails to 0 nuclei; gated in the pipeline) AND library-side as
  `MS_Helper_function.signal_level_from_czi` / `is_czi_effectively_empty` (for
  pipelines that need the check without the analysis repo). A near-black stack
  otherwise produces *spurious* segmentations (Otsu on noise gave 342 "nuclei"
  once) — hence the guard.

- **Airyscan HQ is RAW (pre-reconstruction) over the API.** The Airyscan image
  experiment (`NfS_image_nuceli_Airyscan_001`, FastAiryscan / Line-multiplex)
  produces a CZI with an extra **H axis** (the multiplex / pixel-reassignment
  sub-frames — H=4 here, NOT the 32-element detector array). Each sub-frame is
  **dim** (uint8 max ~10–27) because the light is split and the scan is fast — this
  is normal; it only becomes a high-res, high-SNR image after **ZEN's Airyscan
  reconstruction** (the gRPC API does not run that). So judge Airyscan signal on the
  reconstructed image, not the raw stack (and don't apply the empty-stack signal
  check to raw Airyscan); if the reconstruction is too noisy the fix is in the base
  experiment (laser / dwell / averaging), not the pipeline. Stage navigation
  (authored-P1 region) works identically to the confocal HQ base.

- **ZEN output naming uses the DEFAULT folder, not your custom one.** ZEN writes
  every acquisition to its own default output folder first (query it; here it
  resolves to `F:\UserData\mike\temp`) and enforces output-name **uniqueness there**.
  We then move the file to the caller's folder. The collision counter
  `_unique_czi_name` checks the *custom* folder, so a stale file with the same name
  left in ZEN's *default* folder still causes `ALREADY_EXISTS` — a **known latent
  bug** (see TODO / TEST_PLAN §12). Use unique output names if you can't guarantee a
  clean default folder.

- **The cleanup deletes everything in ZEN's default folder — WON'T FIX.**
  `_move_result_and_cleanup` globs `*.czi` in ZEN's default output folder and
  **unlinks every file except the one just acquired**. That folder is ZEN-owned: it
  is ZEN's configured default image-save location, fetched live via
  `GetImageOutputPath`, and the ZEN-API exposes **no setter** for it — so the
  pipeline cannot point ZEN at a private scratch dir from code (only ZEN's own
  options can). Treat ZEN's default output folder as owned by the pipeline while a
  run is in progress; if you must keep other CZIs on that machine, configure ZEN's
  default save location to a folder nothing else writes to.

- **Keep the acquisition output folder on the same drive as ZEN's default output**
  (`F:`). Cross-drive moves work (`shutil.move`), but same-drive is a fast rename.

- **`ZEN.Error.log.xml` is mostly benign noise.** Thousands of
  `FindCameraOfTrackSetup … Could not determine the used Camera` WARNs are normal for
  confocal/LSM tracks (no camera on the track). A handful of
  `Command not allowed while device in motion (0x0D)` warnings on
  `MTBFocusStabilizerLiveCellScanner` come from firing DF/Z/objective commands
  back-to-back faster than interactive use; ZEN retries and recovers. Only worry if
  these turn into *failures* — then add a small settle delay or back off on 0x0D.

---

## Code / infrastructure traps

- **Import `zeiss_paths` FIRST.** It is what inserts the Zeiss tree onto `sys.path`
  so `import zen_api` (and the `MS_zenapi_*` modules) resolve. Any `import zen_api` —
  or a `pytest.importorskip("zen_api")` — placed *before* `import zeiss_paths` will
  silently fail/skip even when the tree is present. (This bit us once in `conftest.py`.)

- **`.gitignore` blanket-ignores `*.czi` / `*.czexp`** (acquisition outputs). The
  test fixtures are committed only because of explicit negations for
  `tests/fixtures/...`. If you add a new fixture type or location, add a matching
  negation or it won't be tracked. `overview.md` is intentionally ignored
  (regenerate, not shared) — edits to it do **not** travel with the repo.

- **Fixture optics differ.** Only `tests/fixtures/czexp/zstack_LSM.czexp` is a real
  **confocal** experiment with the LSM detector crop fields (FrameSize / Zoom /
  Sampling / ScaledImageRectangleSize …). The others (`snap_single`, `zstack_small`,
  `positions_384`, the SWAF ones) are **widefield** and lack those fields, so the
  `MS_czexp_editor` LSM/crop functions can't run against them — the unit tests use
  synthetic XML for that exact-value coverage.

- **SWAF can load by path or by name.** `run_swaf_from_path` /
  `run_software_autofocus_from_path` load a standalone `.czexp` (portable). The
  by-name `run_swaf` / `run_software_autofocus` still require the experiment to exist
  in ZEN's stored library.

- **`check_experiment_api` imports an experiment ZEN can't unload.** ZEN's
  `ExperimentService` exposes no delete-by-id or unload (`delete` takes only an
  experiment *name*, and an imported experiment has none), so each
  `check_experiment_api` call leaves one imported experiment loaded in ZEN.
  Acceptable because it is now an occasional smoke test, not a per-image call
  (the per-image path is the lean `run_experiment_by_name`). Revisit if/when ZEN
  adds an unload API.

- **Units.** Internally everything is in **metres**; `.czexp` and `.czi` store
  **µm**. Conversions happen at the boundary (`× 1e6` / `÷ 1e6`).

---

## Positioning an acquisition at a target XY(Z) — two ways (don't mix them)

There are two valid patterns for acquiring at a particular XY(Z):

1. **Direct move, then run a position-less czexp.** Move the stage there yourself
   (`ms.move_stage_to_new_xy_position`, plus focus/Z), then run a `.czexp` that has
   **no** position — it acquires around the CURRENT stage position.
2. **Position-in-czexp.** Write the XY(Z) into the `.czexp` as the acquisition
   position and execute it with a **position-aware** run path so ZEN drives the
   stage to it.

**Method 2 DOES work with `run_experiment_from_path` — but the stage MOVES BACK after
acquiring (verified on the scope, June 2026).** Running a `.czexp` whose
`<SingleTileRegion>` is an authored, used position (e.g. `NfS_image_nuceli_002`: P1
with `IsUsedForAcquisition=true`) drives the stage to that position, acquires, and
then **returns the stage to where it was before**. Watching ZEN: the stage visibly
moves to the programmed P1, images, and moves back.

**Gotcha that this creates:** reading the live stage XY *before* and *after* the run
gives the **same** value (the pre-run position), which looks like "it never moved" —
but it did; it just came back. To check WHERE an experiment actually acquired, inspect
the **CZI image content / scene metadata**, NOT the live stage position.

**Authored vs injected position:** `_002` has an authored P1 (added in the ZEN UI, so
the experiment is in use-positions mode) and navigates. An experiment with an empty
`<SingleTileRegions/>` that we merely inject a region into (e.g. `_001` via
`add_single_tile_region`) may NOT be honoured for navigation — set the position by
editing an **already-authored** position instead (`set_position` on `_002`). Verify
per experiment by image content.

---

## Decisions (persistent)

### Project-tracking policy

`smartmic_poc` is the **only** project tracked/shared in this repo. Every other
folder under `projects/` is local-only — facility- or user-specific work that
should not ship with the shared library. Since 2026-07-06 this is enforced by a
**blanket** `.gitignore` rule (`projects/*/` with `!projects/smartmic_poc/`
un-ignoring the public one), so a new local project needs **no manual
`.gitignore` entry** — it's ignored by default the moment it's created.

Each local project should have its **own independent git repo** (`git init`
inside the project folder — not a submodule of SmartMic) and its own
project-local `TODO.md`. Current local projects: `projects/HD_Nuclei_from_slide/`,
`projects/Marc_SM/`, `projects/Slide_Search/` (git repos added retroactively for
the first two ~mid-2026; `Slide_Search` got its repo + `TODO.md` on 2026-07-06 —
before that it sat untracked-but-not-ignored, a gap this blanket rule closes for
future projects). The README's projects table therefore lists only the public
PoC. `sandbox/` (exploratory / scratch scripts) is also git-ignored; those
scripts add the repo root to `sys.path` themselves so they can still import the
core modules.

### Stage speed / acceleration (investigated 2026-06-26, implemented 2026-06-30)

The API supports it (`zen_api.lm.hardware.v2`: `SetSpeed` / `SetAcceleration`, both
% `[0, 100]`); it is **not** in the `.czexp` (it is hardware-layer device state, not
a per-experiment setting). Both are set TOGETHER once per run from inside the shared
`preflight()` — for a live sample it is the **acceleration** (jerk at move
start/stop) that sloshes the medium, so a low top speed paired with a high
acceleration would still jolt the sample. **Default 100%/100%** (full speed = normal
project); turn down only for a sensitive/live sample. **No restore** (restoring would
defeat the purpose). The values do **NOT** persist across a ZEN restart — fine, since
`preflight` re-sets them every run. Implementation: `MS_zenapi_stage_LM.set_stage_motion`
(+ defaults `STAGE_TRAVEL_SPEED_PERCENT` / `STAGE_ACCELERATION_PERCENT`),
`MS_CD7_API_LoA.set_stage_motion_sync`, called from `preflight`.

**HARDWARE UPDATE (2026-06-30): this gateway REJECTS SetSpeed/SetAcceleration.**
First real hardware exercise of this path (Slide_Search run, slide insert carrier)
returned `FAILED_PRECONDITION: "This parameter is not supported by the device."` —
so the API *defines* the RPCs but the device/gateway here does not honour them. The
earlier "implemented & works" claim was code-complete but never hardware-verified.
Consequence: `preflight` no longer aborts unconditionally on a stage-motion failure.
It aborts only when a throttle was **explicitly requested** (speed/accel not None —
the sensitive/live-sample case, where silently running at full speed would be
unsafe); when no throttle was requested (defaults → full speed, no protection
intended) it logs a WARNING and proceeds at the stage's current motion.

**CORRECTED 2026-07-14: it's SPEED only, not both.** Re-tested each RPC
individually against the live gateway: `GetSpeed`/`SetSpeed` reject outright
(`FAILED_PRECONDITION`, same message as above), but `GetAcceleration`/
`SetAcceleration` **work fine at arbitrary values** — verified by setting
acceleration to 40%, reading back 40%, then restoring 100% and reading back 100%.
The 2026-06-30 note above over-generalized from one combined failure (the two
RPCs were called back-to-back, and the first one to raise — SetSpeed — masked the
fact that SetAcceleration, called just before it, had already succeeded).

Since acceleration is the parameter that actually protects a live sample (see
above — it's the jerk, not the top speed, that sloshes the medium), this is real
throttling capability, not a total loss. `MS_zenapi_stage_LM.set_stage_motion` now
applies speed and acceleration **independently**: an acceleration failure still
raises (it's the one thing this function guarantees), but a speed failure is
caught, logged, and tolerated — it returns `speed_x`/`speed_y` as `None` rather
than blocking acceleration from being applied. `preflight` reflects this: a
throttle request now only aborts if acceleration itself can't be set; a
speed-only rejection is noted in the log line ("speed throttle not supported by
this device — acceleration only") and preflight still passes. A live-sample run
requesting a throttle today gets real acceleration protection, just not a speed
cap — re-check whether speed support ever lands at the ZEN 3.14 upgrade.

**Confirmed 2026-07-14: the `FAILED_PRECONDITION` message is misleading — it is
NOT a hardware limit.** Checked whether ZEN's own UI can set stage speed on this
scope: **it can** (speed and acceleration both, from the ZEN UI). So the stage
controller genuinely supports live speed changes; the gRPC gateway is choosing
not to expose/honour `SetSpeed`/`GetSpeed` for this configuration, despite the
error text framing it as a device incapability. Likely an API/gateway-layer gap
(missing wiring for this hardware profile, a permissions/scope restriction, or
simply not implemented yet in this gateway version) rather than something
permanently off the table — worth re-testing whenever the gateway or ZEN itself
is updated, not just filed away as "the hardware can't do it."

### Incubation — temperature / CO2 / N2 (investigated 2026-06-26)

**DECISION: do nothing.** Incubation is set manually on the incubator, which is
acceptable — no control and no monitoring will be built. Key finding (so we don't
re-investigate): incubation is **not reachable over the ZEN gRPC API** — no
incubation/temperature/CO2/climate service exists in any `zen_api` stub, and the API
cannot run a ZEN macro / OAD script (no `ExecuteMacro`/`RunScript`; the ZEN-API
README says it is "not a replacement for OAD"). If ever revisited, the path would be
read-only monitoring via the incubator vendor's own SDK (identify the make/model
first — likely Okolab / Ibidi / Pecon), independent of ZEN.

### Image processing / analysis is NOT (really) available over the API (investigated 2026-07-15)

**DECISION: do nothing for now — keep the existing subprocess-based external-analysis
approach (`MS_image_analysis.py`).** Wanted to know whether ZEN's own image
processing (deconvolution, stitching, segmentation, export, ML inference) could be
triggered via the gRPC API instead of shelling out to separate analysis repos.

**No per-operation processing RPC exists.** Grepped the entire `zen_api` stub tree
(both the OLD loose `python_examples/zen_api` and the NEW
`python_package/zen_api-2025.10.1/src/zen_api`, both present on this machine — see
"ZEN version" note below for the shadowing gotcha) and the 6300-line
`ZEN_API_Documentation_2025.10.1.md`/`.pdf` reference for `deconv`/`stitch`/`segment`:
zero matches anywhere.

**What DOES exist: a generic job-template runner**, `zen_api.workflows.v3beta.WorkflowService`
(`LoadJobTemplate` / `RunJob` / `StartJob` / `StopJob` / `WaitJob` /
`GetAvailableJobTemplates` / `IsJobRunning` / …) plus
`zen_api.workflows.v1beta.JobResourcesService` (typed get/set of named parameters —
e.g. `res_image_processing_sigma_x` — on the currently loaded job). Zeiss's own
example (`python_examples/zencore_jobs/RunJob.py`) loads a template named
`"ZEN-API Job"` and tweaks its params this way. **The catch**: the job's actual
content (load → process → save) has to already be authored in **ZEN Core's own Job
Designer UI** and saved as a `.czjob` XML file on the machine (confirmed by
unpacking the shipped example template: a `Task.Common.Processing` /
`Tool.Common.Gauss` task graph). The API can load/parameterize/run an EXISTING
template — it cannot compose a new processing pipeline from Python, and can't
introspect what a template actually does beyond its name/category/description.
Whether a template could wrap a licensed Deconvolution/Stitching module is
possible in principle but not something the gRPC layer exposes either way.

**Zeiss says so directly**: `ZEN-API/README.md` (line 98): *"as of right now OAD
offers a lot more functionality, which is not directly available via ZEN API (yet).
For example all the ZEN internal image processing and image analysis function are
not integrated into ZEN API"* — same shape as the incubation scoping note below.
Consistent with that, Zeiss's own processing example scripts
(`processing_tools.py`, `segment_objects.py`, `onnx_inference.py`, `MS_cellpose.py`
— siblings of the scripts SmartMic's `MS_zenapi_*.py` files are based on) don't
import `zen_api` at all; they pull pixel data client-side (via `zenapi_streaming.py`'s
`PixelStream`, or by reading `.czi` directly) and process it locally — exactly
SmartMic's own `MS_image_analysis.py` subprocess pattern.

**Not pursued**: `WorkflowService` is only useful if a `.czjob` template already
exists in ZEN for the exact operation needed, and no such template exists /
is needed here today. **Revisit at a future ZEN-API release** — Zeiss's own
"(yet)" wording suggests processing/analysis integration is roadmapped, not ruled
out.

### ZEN version is NOT available over the API (investigated 2026-06-30; resolved out-of-band 2026-07-14)

We wanted to log the running ZEN version at `preflight`. It can't be done through
the gRPC API: enumerating every RPC on all 27 service stubs turned up no
version / about / system-info call. The only "version" string in `zen_api` is
`minimum_required_version` on the composition-module message (the version a
*module requires*, not the running app). The shipped stubs also carry no package
version (`zen_api.__version__` is absent, no pip metadata, no VERSION file). Same
shape as incubation — the API is "control from outside" and exposes no application
metadata.

**What we DO log for this (2026-06-30):** the identity of the `zen_api` gRPC
*stubs* in use, via `zeiss_paths.zen_api_version()` → `(version, path)`. The NEW
layout ships the stubs as `…/python_package/zen_api-<version>/src/zen_api`, so the
version is parsed from that folder name; the OLD loose layout
(`python_examples/zen_api`) has none → version `None`.

**Resolved 2026-07-14 — both the ZEN app version and the gateway service version
ARE available, just not through the gRPC API:**

- **`zeiss_paths.zen_app_version()`** — ZEN stamps its own version
  (`SoftwareVersion`, e.g. `"3.13.109.08000"`) on the `<properties>` of almost
  every event it logs to its own log file, `C:\ProgramData\Carl Zeiss\Logging\ZEN.log.xml`
  (path: `zeiss_paths.ZEN_LOGGING_DIR`, overridable via `SMARTMIC_ZEN_LOGGING_DIR`).
  That file is NOT one well-formed XML document — it's a flat stream of one
  `<event>...</event>` fragment per line with no wrapping root element — so it's
  read as text, not parsed as XML. Only the file's TAIL is read (it grows for as
  long as ZEN runs, several MB), and the LAST `SoftwareVersion` match in that tail
  is used, so a version that changed mid-file (a restart/upgrade) reflects what's
  running now.
- **`zeiss_paths.zen_api_gateway_version()`** — the FileVersion of
  `ZenApiGateway.exe` itself (path: `zeiss_paths.ZEN_API_GATEWAY_EXE`, overridable
  via `SMARTMIC_ZEN_API_GATEWAY_EXE`), read via the Win32 file-version resource
  (`win32api.GetFileVersionInfo` — already present transitively in the `smartmic`
  env via napari/magicgui's Windows deps, no new dependency needed). This is
  arguably the MORE useful of the two: it's the version of the actual gRPC service
  SmartMic talks to, so it tracks the API contract more directly than the ZEN
  *application* version does (the gateway can be serviced somewhat independently
  of the rest of ZEN).

Both are logged together with the `zen_api` stub identity in `preflight`'s step 0,
so a support question ("what were you running when this broke?") has a complete
answer. The `HKLM\SOFTWARE\Carl Zeiss` registry route mentioned in the original
2026-06-30 note was never tried — the log-file / FileVersion routes above turned
out to work fine, so the registry idea is moot unless those ever stop working.

**Gotcha found while implementing:** on this machine there are also 4 side-by-side
installed **MTB 2011** versions (the hardware-control layer under ZEN, which
SmartMic never calls directly) — `3.1.11.0`, `3.7.10.0`, `3.12.9.0`, `3.13.7.0` —
each with its own log folder (`MTB2011_<version>/`). The newest-installed
(`3.13.7.0`) is NOT necessarily the one actually running: checking log-freshness
on 2026-07-14, `3.12.9.0`'s log was being written live while `3.13.7.0`'s was over
a day stale. **Deliberately not logged** — no automation-surface reason to need
it today — but if it's ever added, do NOT assume "newest installed = active";
check which log folder is actually being written to, the same way `zen_app_version`
picks the tail of the actively-growing file rather than guessing from a folder name.

**Extended 2026-07-14 to SmartMic's own version too:** while at it, added
`zeiss_paths.smartmic_version()` — SmartMic has no `__version__` and no git tags
yet (`pixi.toml`'s `version = "0.1.0"` is a static, never-bumped placeholder), so
this shells out to `git describe --tags --always --dirty` against SmartMic's own
`THIS_DIR` (not the caller's cwd, so it's correct regardless of which project with
its own separate git repo is importing it live). Today that's just an abbreviated
commit hash (+ `-dirty` if the tree has uncommitted changes, as it will most of
the time during active development); it will start returning a real version
string automatically once `PROJECT_CHECKLIST.md`'s freezing workflow actually
tags a release — no code change needed then. Returns `None` for a
vendored/frozen copy of the modules that's no longer inside SmartMic's own git
repo. All four version identities (SmartMic, ZEN app, ZenApiGateway, zen_api
stubs) are now logged together in `preflight` step 0.

**Gotcha observed on this machine:** both layouts are present —
`python_package/zen_api-2025.10.1/` exists, but `import zen_api` resolves to the OLD
loose `python_examples/zen_api` copy, because `zeiss_paths` appends `python_examples`
to `sys.path` BEFORE the versioned-package `src`. So the loose (unversioned) copy
shadows the 2025.10.1 package, and the preflight line reads "unversioned (loose
layout)". That's why we log the *path* too, not just the version. If you want the
versioned package to win, remove/rename the loose `python_examples/zen_api` copy (or
reorder `zeiss_paths` to prefer the `python_package` src) — not changed here, since
the loose copy may be intentionally the one matched to the gateway.

---

## Reference

### Optics map (this CD7)

| Objective pos | Lens | | Optovar pos | Tubelens |
|---|---|---|---|---|
| 1 | Plan-Apochromat 20×/0.7 | | 1 | 2× |
| 2 | Plan-Apochromat 5×/0.35 | | 2 | 1× |
| 3 | Plan-Apochromat 20×/0.95 | | 3 | 0.5× |
| 4 | Plan-Apochromat 50×/1.2 (immersion) | | | |

Tests use **5×/1× (pos 2/2)** for low-risk stage moves and **20×/0.95 + 2× (pos 3/1)**
for focus/SWAF (the bundled SWAF experiments are tuned for the 20×). Position 4 is the
50× **immersion** objective — `set_objective_set_optovar_sync` applies longer
timeouts/retries when moving to/from it.

### Per-run output folders

Each run writes to `F:/UserData/api/run_<YYYYmmdd_HHMMSS>/` (image/analysis/log
sub-folders per stage), so separate runs never mix or overwrite. The folder name has
**1-second resolution**, so two runs started in the *same second* would share one
folder and interleave outputs — **left as-is on purpose** (a run takes minutes;
launching two within one second is not realistic). If that ever changes (e.g. a
scheduler firing parallel runs), add a short uuid/PID suffix to the run-folder name.
`main()` verifies every required `.czexp` + the positions file exists *before*
touching hardware and aborts with a listed error if any are missing (they are loaded
BY PATH from vendored copies, and the per-position acquisition calls are not all
individually guarded).

### Image-analysis repos (producer ↔ consumer mapping)

The analysis half lives in **separate repos**, each with its own pixi env, launched
by `MS_image_analysis.run_analysis` as a subprocess. Each analysis and its
SmartMic-side consumer must agree on the result-JSON filename:

| Analysis repo | Result file(s) | SmartMic consumer |
|---------------|----------------|-------------------|
| [Mike587/ia_PoC_002](https://github.com/Mike587/ia_PoC_002) (nuclei) | `<prefix>_targets.json` | `projects/smartmic_poc/MS_SmartMic_PoC.py` (public) |
| `ia_Marc` (spheroids, local) | `<prefix>_spheroids.json`, `<prefix>_spheroid_3d.json` | `projects/Marc_SM/MS_Marc_SM.py` (local-only) |
| `ia_NfS` (tissue/thickness/nuclei, local) | `<prefix>_tissue.json`, `<prefix>_thickness.json`, `<prefix>_nuclei.json` | `projects/HD_Nuclei_from_slide/MS_HD_Nuclei_from_slide.py` (local-only) |

Note: `targets.json` is the contract for analyses built from the ia_PoC_002 scaffold;
Marc's pair predates it and keeps its own names. Not enforced globally — each
producer/consumer pair just has to match.

### Dependency history: `zen_api_utils` vendored out

SmartMic originally relied on a handful of `zen_api_utils` helpers
(`initialize_zenapi`, `set_logging`, the objective/optovar position lookups, and the
SWAF demo helpers). `zen_api_utils` is hand-written example glue that ships only
inside the ZEN-API `python_examples` folder and is **not** a packaged dependency.
Those helpers were vendored into `MS_zenapi_helpers.py`, so the library now depends
only on the packaged `zen_api` gRPC stubs and no longer needs `zen_api_utils` on
`sys.path`. See `copyright.md` for the Zeiss attribution preserved during vendoring.
