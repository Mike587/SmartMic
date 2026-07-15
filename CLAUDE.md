# CLAUDE.md

Guidance for Claude Code (or any AI assistant) working in this repository.

## What this is

SmartMic is smart-microscope automation built on the Zeiss ZEN gRPC API
(ZEN-API), controlling a physical Zeiss CD7 microscope: XY stage, Z/focus
drive, objective/optovar changer, and experiment acquisition. The repo root
is a reusable API wrapper ("library"); example pipelines that consume it live
under `projects/`. Full architecture, layout, and module table: **[README.md](README.md)**.

**This code drives real hardware.** A wrong unit, an unguarded move, or a
swallowed error can crash an objective into a slide/stage or break the
immersion water bridge. Read **[DEV_NOTES.md](DEV_NOTES.md)** before changing
anything in `MS_zenapi_stage_LM.py`, `MS_zenapi_focus.py`,
`MS_zenapi_objectivechanger.py`, or `MS_CD7_API_LoA.py` — it's the record of
hardware/ZEN gotchas (immersion, DefiniteFocus quirks, the stage-speed
gateway rejection, units, ZEN-version discovery) and the persistent design
decisions behind them.

Other tracked docs: **[tests/README.md](tests/README.md)** (how to run
tests) and **[tests/TEST_PLAN.md](tests/TEST_PLAN.md)** (tiers, fixtures,
design).

## Local-only docs (not present in every checkout)

A few docs are maintainer notes and are still **git-ignored** — they exist on
this machine but won't be there in a fresh clone. If they're present, read
them; if not, don't assume the knowledge they'd contain is captured elsewhere:

| File | Contents |
|------|----------|
| `TODO.md` | Open, forward-looking tasks for the shared library (project-specific TODOs live under each `projects/<name>/TODO.md`). |
| `copyright.md` | Which files are derived from Zeiss ZEN-API examples and must keep the Zeiss copyright header vs. original ETH/ScopeM work. Check before adding a new `MS_*` file or editing headers. |
| `overview.md` | Generated function-by-function map of the repo. Regenerate rather than hand-edit; it will drift from the code. |
| `PROJECT_CHECKLIST.md` | The freeze/vendor workflow for projects under `projects/` (how a project should pin itself to a SmartMic commit once it's done). |

## Layout in one line

- Repo root `MS_*.py` + `zeiss_paths.py` = the library (imported as `ms`-style wrappers).
- `projects/smartmic_poc/` = the only project tracked/shared in this repo.
- Every other `projects/<name>/` folder is **local-only** (git-ignored by a
  blanket rule), has its own independent git repo, and its own `TODO.md` /
  dev-notes file (e.g. `projects/Slide_Search/dev_notes.md`,
  `projects/Marc_SM/handover.md`). If you're asked to work in one of those,
  read that project's own docs first — don't assume repo-root conventions
  carry over verbatim (each project has its own optics, carrier, and safety
  constants).
- `sandbox/` = git-ignored scratch/test scripts, not part of the library.

## Safety-critical invariants (do not casually "fix")

- **Units.** Internally everything is **metres**; `.czexp`/`.czi` files store
  **µm**. Conversions happen only at that boundary (`× 1e6` / `÷ 1e6`). A
  missing or doubled conversion is a real-hardware bug, not a cosmetic one.
- **Z=0 breaks immersion.** On this (inverted) scope, lowering the Z-drive to
  0 fully retracts the objective and drops the 50× immersion water bridge.
  `move_stage_to_new_xy_position` deliberately **raises** rather than moves
  when the immersion objective is active — don't "fix" this into a silent
  move at a hardcoded travel Z; that's exactly the delicate geometry ZEN's
  own experiment-driven moves already handle safely.
- **The stage-speed gateway rejects `SetSpeed`/`GetSpeed`** on this
  configuration but accepts `SetAcceleration`/`GetAcceleration` — this was
  confirmed by testing each RPC individually, not assumed. `preflight()` and
  `set_stage_motion` treat these independently on purpose (see `DEV_NOTES.md`
  if present, and the git history around `set_stage_motion`/`preflight`).
  Don't re-couple them without re-testing on real hardware first.
- **`import zeiss_paths` before anything that touches `zen_api`.** It's what
  puts the Zeiss tree on `sys.path`; importing `zen_api` (or
  `pytest.importorskip("zen_api")`) first silently fails/skips.
- Hardware tests only run with `--run-hardware`; a plain `pytest` must never
  move the microscope. Don't change that default gating.

## Running & testing

```
pixi run -e smartmic poc                 # run the PoC pipeline
pixi run -e smartmic test                # offline unit tests (no scope)
pixi run -e smartmic test-hw             # hardware tests, opt-in, on the scope
```

See **[README.md](README.md)** for `config.ini` setup (git-ignored, holds a
control-token secret — never commit it) and how the Zeiss `zen_api` dependency
is resolved. See **[tests/README.md](tests/README.md)** for the full test
workflow and safety order.
