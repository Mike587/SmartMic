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

- **What each DF RPC does, per objective (measured 2026-11-09).**
  - **Setup:** project `DF_Exploration` (local), one XY in a water-filled 6-well glass-bottom plate ("Multiwell 6", glass 168 µm), 2x tube lens. Raw `DefiniteFocusService` calls, a Z trace polled at about 60 ms, and DF events from `ZENService.log.xml`.
  - **Where to see what DF did:** the API returns no quality, but the log does: `MtbStabilizingCycleFinishedEvent(): mtbResult, signalQuality, focusPosition [µm], errorCode`. The log also shows the firmware command behind each RPC (below), `FocusPositionLimitParameter` 4500, and the carrier bottom thickness in nm.
  - **`FindSurface`** (SetOffset → Finding). It always ends on the surface, and the returned `zposition` equals the drive read-back.
    - 5x: a fast sweep **up to about +410 µm above the surface** (+590 µm from −500 µm), then down. 2–3 s, ±0.5 µm.
    - 20x/0.7 and 20x/0.95: a staircase **from below** (about 115–125 µm steps, 0.5 s each). From above, it first drops to −55 µm. Never more than +2.3 µm above the surface. 3–4 s near the surface, about 9 s from Z=0. ±0.15–0.25 µm.
    - 50x W: from above (+5…+30 µm), down to the surface in 2–3 s, ±0.07 µm. From below it climbs (about 20 µm steps) to the **first reflex above the start** (see the underside entry below).
    - The 30 starts on 5x and the 36–40 on each 20x, from −1000 µm up to +50 µm above the surface, all found the same surface.
  - **`StoreFocus`** = `InitOnCurrentPosition`. On 20x/50x it takes about 0.45–0.9 s, does not move (50x: a brief +6 µm lift), and logs `MtbFocusPositionOffsetChangedEvent(offset, Calculated)`, i.e. it stores the **offset to the reflex**. On 5x it instead does a full sweep and stores **no** offset.
  - **`RecallFocus`** = `FindFocus` → "Stabilizing" (a short local cycle). On 20x/50x it **returns to reflex + stored offset to ±0.07–0.15 µm**, from above or below, also after a FindSurface. On 5x it goes to the reflex (offset ignored, ±2 µm). After an objective change it falls back to a full FindSurface.
  - **`LockFocus(timeout)` is rejected on every objective:** gRPC INTERNAL within ms, without reaching DF. Nothing is logged except the gateway forward. **`UnlockFocus` is a no-op.** Continuous stabilisation is therefore only possible inside an experiment (`.czexp` `DefiniteFocus` / `DefiniteFocusStabilize`).
  - **`FocusService.Stop` does not stop a DF search.** On 5x Z kept rising; DF then reports `ErrFocusStabilizer (0x29)` even though it found the surface with quality 99. **0x29 = DF interrupted.** So no software guard can brake a DF search; only its start Z decides where it goes.
  - **Image focus vs DF surface** (DAPI dirt on the well bottom):
    - 5x: about −12 µm (broad depth of field);
    - 20x/0.7: about +1 µm;
    - 20x/0.95: about +1.5 µm;
    - 50x: about +1 µm.
    - The CZI `FocusPosition` equals the drive Z exactly (widefield).
    - At 50x + 2x, `compute_focus_score` (raw Laplacian) only sees pixel noise. Bin 4×4 first.
  - **Switching to the 50x over the API applies the water automatically.** `set_optics_verified(4, …)` took 70 s: ImmersionAdapter "Creating", pump cycles, `IsImmersionApplied = True`.
  - **Library change (2026-11-09):**
    - `definite_focus_find_surface` / `run_definite_focus_find_surface` do StoreFocus only with `store_focus=True` (default False). Use it only before `run_definite_focus_recall`.
    - After a failed search they do not store anything. Before this change, they stored the current Z, and an offset recorded at a random Z sends a later RecallFocus to an incorrect Z.
    - The automatic StoreFocus after each search cost 0.45 to 1.8 s. On the 5x it also did a +414 µm sweep.
  - Seen, not confirmed: the surface drifted about +1.5–3 µm/h, and the first FindSurface right after an objective change was 3–4 µm off (2 cases).

- **50x: DF locks onto the plate UNDERSIDE with quality 99 if started too low (reproduced 2026-11-09).**
  - Same setup, well bottom S = 1192.4 µm. From S −30/−60/−100 µm, FindSurface climbed and found S (quality 96). From **S −170 µm it locked at S −149.16 µm** (the water–glass interface on the objective side; 170 µm × 1.33/1.52 = 149 µm) with **quality 99 and "Success"**. Same signature as the New-03 C7 failure (below). Over the API, DF took the first reflex above its start. That does **not** explain New-03 itself: there DF started *between* the two interfaces (43 µm above the underside, 109 µm below the well bottom) and still went down to the underside. Here, starts between them (−30…−100 µm) climbed to the well bottom. The difference could be the in-experiment DF (FocusStrategy) or the stored-reference state; not yet tested.
  - Right after switching to the 50x, a FindSurface from S −292 µm went **down** instead (no reference yet: `offsetToRestore = 0`). It ran to **−1000 µm** in 31 s and found nothing (ReflexNotFound), which **breaks the water bridge**. Re-immersion in ZEN fixed it.
  - **Rules for the 50x:**
    1. Seed DF **above** the expected surface (+5…+30 µm always found S).
    2. Never start more than about 149 µm below it.
    3. Never search blind after the objective change.
    4. Check every result against the expected Z: about −149 µm means the underside.
  - `DefiniteFocusTracker`'s adaptive start (last surface −100 µm) violates rule 2 as soon as the next surface is more than about 49 µm higher. See TODO.

- **Dry objectives: DF finds the plate UNDERSIDE and adds an offset from the carrier's bottom thickness (measured 2026-11-09).**
  - **Test:** 20x/0.7, same XY, carrier bottom thickness 250 µm and then 170 µm (same skirt).
    - With 250 µm DF parked at 1202.7 µm, but the dirt on the well bottom was sharp **48 µm lower**.
    - With 170 µm, DF and the image focus agreed (±2 µm).
    - Both results had quality 99.
  - **Scale:** about **0.55–0.6 µm Z per µm of thickness error**. A thickness of 0 parks DF on the air–glass underside (seen in the log: 94 µm below the 170 µm result on 5x).
  - **Where the thickness comes from** (`MtbBottomPropertiesChangedEvent` in `ZENService.log.xml`): the active sample carrier.
    - That is the template value, **unless "measure bottom thickness" is ticked at plate load**. Then ZEN measures the glass and the measured value replaces the template value (checked: a 250 µm template with the measurement on gave 168 µm and correct focus).
    - It does not come from the objective correction collar ("Cover bottom thickness", 130–210 µm), which ZEN sets from the same value.
    - It does not come from an API-run experiment's template, which leaves DF unchanged.
  - **The load measurement:**
    1. ZEN sets the DF thickness to **0** and runs FindSurface at a detection point, which parks DF on the air–glass underside.
    2. It runs a hidden `CoverGlassDetectionExperiment` (a camera Z-stack, about 12 s).
    3. It sends the measured thickness to DF.

    This is the "thickness 0" in the log. A DF search of your own started inside that window parks on the underside.
  - **Consequences:**
    1. On dry objectives, the DF focus is only as good as the bottom thickness. Use "measure bottom thickness" at load, or a correct template. An error of 10 µm moves focus by about 6 µm, and quality does not show it.
    2. Do not start DF while a carrier is loading.
    3. The measurement is made at one point. A well-to-well variation of glass thickness still goes straight into the DF focus.
  - **The carrier MATERIAL matters even more.** Same glass plate and XY on 20x/0.7, material switched Glass → Polystyrene in the sample setup:
    - ZEN rescaled the thickness to keep the measured optical thickness (168 × 1.594/1.52 = 176.18 µm).
    - DF then parked **137 µm below the real surface** (1022.0 against 1159.1 µm, quality 99), below the plate, in air, with nothing in focus.
    - The search first approaches the underside reflex, then moves about 80 µm **down** instead of up.
    - Switching back to Glass restored 1159.5 µm (DAPI sharp at DF +0.5 µm).
    - **The material must match the plate.** A real polystyrene plate has not been tested.
  - **Library checks (2026-11-09):**
    - `preflight` runs `check_carrier_for_df`. It stops the run if:
      - the thickness is 0;
      - the material is not `expected_material`;
      - the thickness is not in `thickness_range_um`;
      - the value that DF last received (`zeiss_paths.zen_df_bottom`, from the ZEN log) is different from the API value.

      It gives a warning if "measure bottom thickness" is off at tray load.
    - `DefiniteFocusTracker` records the carrier bottom (`carrier_bottom`, API, approximately 10 ms) on the first search. It compares it before each search. A change or a thickness of 0 raises `CarrierChanged`, a subclass of `DefiniteFocusFault`, so the run stops. `reset()` accepts a new carrier.
    - `load_tray_and_prescan` waits (`wait_for_df_bottom`, maximum 60 s) until DF has a non-zero thickness that agrees with the API, two reads in a row.
    - The ZEN log is not read before each DF call: ZEN writes the event only when the value changes, and the logs rotate approximately every 10 min.
    - No check can find a thickness that is stable but incorrect (e.g. an incorrect template). Only the measurement at load or a correct template can prevent that.
  - The 50x W was not tested with a wrong thickness. It has no air–glass interface; there DF parks directly on a water–glass reflex (see the underside entry above).

- **5x↔50x surfaces differ a lot here.** The 5x tissue-scan surface read ~1189–1194
  µm; a 50x DF (seeded at 1080 µm) landed ~939 µm. Treat the per-objective surface
  Z as independent — never assume 5x and 50x focus at the same Z.
  (On the 6-well glass-bottom plate, 2026-11-09, the opposite held: 5x about 1201 µm,
  20x about 1190 µm, 50x about 1192 µm. The offset depends on the plate.)

- **ZEN computes the tile grid from the CURRENT objective/optovar at RUN time.**
  Launching an experiment before a slow optics change has propagated (especially
  moving OFF the immersion objective) makes ZEN tile for the stale config → wrong
  per-tile FOV/overlap (a doubled, ghosted mosaic). Verify the read-back AND let it
  settle before acquiring: `MS_CD7_API_LoA.set_optics_verified` (since
  2026-10-14; promoted from HD_Nuclei's / Marc_SM_002's `ensure_optics`). The
  cause (read-back leading the acquisition engine) is inferred and was only seen
  when leaving the 50x immersion objective, so the settle wait is a cheap safe
  default, not confirmed ZEN behaviour. See TODO for a possible ZEN-native
  "defined config" fix being investigated.

- **ZEN sometimes reports a successful move as failed (spurious "not reached").**
  Learned in Marc_SM_002 (2026-09-28/30). The move raises a gRPC `INTERNAL`
  error, but the drive is on target:
  - **Z:** `Requested position was not reached.` on ~0.7 % of cells (3 of
    ~425 on 2026-09-28), read-back 0.00–0.01 µm off the target. Not tied to
    the move size (0.01, 0.33 and 1.69 µm moves). Always the final move to the
    mid-plane right after a z-stack.
  - **XY:** `Position could not be reached.` 11× in ~1600 moves
    (2026-09-29/30). Every failure was a tiny 2–10 µm hop (~1.4 % of those
    hops); none of the ~800 long moves failed.
  - No matching MTB2011 hardware-log event, so it is a ZEN/gateway-level
    report, not a drive fault.

  Fix: an optional `tolerance_m` on `MS_CD7_API_LoA.move_focus_to_new_z_position`
  / `move_stage_to_new_xy_position`. On a ZEN (`GRPCError`) error the position
  is read back: within the tolerance → done; otherwise one retry. The
  read-back never equals the target exactly, so the tolerance is the caller's
  parameter (Marc_SM_002: 0.25 µm for Z). The default `None` keeps the old
  behaviour (raise). The immersion refusal is never retried.
  Side fix: `MS_zenapi_focus.move_focus_to_new_z_position` used to log every
  failed move a second time as "Could not get current position: <the move
  error>". That came from a re-raise inside its own try block, and it was
  misleading in the run logs. It now logs that only when the read-back
  itself fails.

- **DefiniteFocus can drop out mid-run (hardware fault).** 2026-09-28 22:14,
  Marc_SM_002: `MTBFocusStabilizerLiveCellScanner: ... not initialized
  (0x0E)` on every attempt from then on. It was preceded by four 2000 ms
  command timeouts in the 8 min before. The run kept going for 2 h, out of
  focus. The MTB2011 logs show recurring DF / CAN-bus communication faults on
  this CD7 since January 2026, so this is a Zeiss service case (report and
  logs in the local `projects/Marc_SM_002/zeiss_service/`). Library answer:
  `MS_CD7_API_LoA.DefiniteFocusTracker` raises `DefiniteFocusFault` after N
  failed searches in a row (default 5). Its adaptive start (100 µm below the
  last surface, ~4 s vs ~11 s for a full search) is the standard way to focus
  a series of positions.

- **DefiniteFocus fails when the immersion water bridge breaks (50x).**
  2026-10-27, Atreya's ZEN run (`F:\UserData\Atreya\2026-10-27\New-03.czi`, 96-well,
  meander between columns 1 and 7). DF reported quality 99 until ZEN's
  auto-immersion renewed the water and the stage then moved 52 mm (but from
  C7 on it had already locked onto the wrong surface, see the next entry). From then
  on DF failed at most positions, every column-7 position included. ZEN
  kept the last good Z, so those wells were imaged out of focus. The
  exact messages (ZENService log):
  `Definite Focus was not able to detect a stabilization signal.
  Stabilization not possible. Check cleanliness of the sample carrier bottom
  and the objective front lens.` (ErrFocusStabilizerMaximum, 0x65, code
  ReflexNotFound), plus `Definite Focus action canceled - focus limit
  reached.` (FocusLimit) and `The stabilization process doesn't converge to
  a final value.` (Oscillation, 0xB4). Unlike the 0x0E fault above, this is
  not a hardware fault; it points to the water bridge (likely, not
  confirmed). Re-immersion is a global ZEN
  option (Tools > Options > Celldiscoverer > Auto Immersion – Reimmersion
  Settings; default 120 min / 200 mm, 10 min minimum). The interval in the
  `.czexp` is ignored on the CD7. Analysis: `Marc_SM_002/SM_Output/New-03_focus_analysis/`.

- **With immersion, DF can lock onto the WRONG surface and still report
  quality 99.** Same run (New-03.czi, 50x/1.2 W, ibidi 96-well, 170 µm glass
  bottom). At C7 FindSurface found the **bottom of the plate** (water–glass,
  objective side) instead of the bottom of the well (glass–medium, where the
  cells are). Both interfaces have about the same index step, so both give a
  clean reflex: quality stayed 99 on all 20 C7/D7 positions, every one
  **150 µm too low** (range −148 to −153 µm against a plane fitted to the good
  wells A–C, residual sd 2.4 µm). 150 µm matches 170 µm glass refocused in
  water (170 × 1.33/1.52 ≈ 149 µm; with air it would be ≈ 112 µm), so the
  water was there. The confocal images from C7 on are completely dark (dark
  level at every Z, every channel) — at 150 µm off the pinhole rejects
  everything, so "dark stack" does NOT mean a laser fault. The software
  autofocus could not fix it (search range ±20 µm); its score fell from
  300–700 to ~50 at that position and nobody looked.
  (Same lock reproduced over the API 2026-11-09, but only from a start
  *below* the underside: from between the interfaces, the API FindSurface
  climbed to the well bottom. So New-03's start alone does not explain it.
  See "50x: DF locks onto the plate UNDERSIDE" above.)
  Why at C7: FindSurface starts at the **previous position's Z**. The plate
  was tilted +107 µm over the 54 mm between columns 1 and 7, so after the
  C1→C7 jump the start (1197) was 43 µm above the plate bottom but 109 µm
  below the well bottom — it took the nearer one. A1→A7 had a similar setup
  (52 vs 98 µm) and happened to pick right. Once wrong, every following
  position started from the wrong Z and stayed wrong.
  Lessons:
  - **DF "Success, quality 99" does not prove the right surface.** Check the
    result against the expected Z (plate plane / neighbouring positions /
    last good Z + tilt); a jump of about one glass thickness (~150 µm at 50x
    W) is the signature.
  - Avoid large Z jumps between consecutive positions: seat the plate flat,
    order wells as a short snake (neighbours, not col 1 ↔ col 7 every row),
    or seed DF near the expected surface.
  - ZEN's **focus surface / support points** may mitigate this inside a ZEN
    experiment (not tested).
  - Idea, not implemented: after DF, take a short **widefield snap and score
    its sharpness/signal**; reject or redo the DF if it is flat. The existing
    dark-stack check (`MS_Helper_function.is_czi_effectively_empty`) would
    also have caught it, but only after the full stack.
  The later failures from D1 on (DF lost after the 04:42 refill, then locked
  ~+135…+160 µm too HIGH, images still dark) are only partly explained, see
  the next entry.
  **Confirmed by re-imaging** (New-04.czi, same plate not removed, 2026-10-27
  morning): at the same XY, New-04 found the well bottom with quality 99 and
  good images; New-03's C7/D7 DF Z was **163.0 µm lower** (13 positions,
  ±0.3 µm). The plane extrapolated from A–C matched New-04's real surface to
  −2…+22 µm (plate not quite flat), so a plane from a few good wells is a
  usable expected-Z check. Write-up: `Mike_Projects/20261028_Ati_CD7_Issues/`.

- **An auto-immersion refill can break the water bridge (seen directly).**
  2026-10-27, New-04/06/07/08 (same plate, 50x/1.2 W). ZEN's periodic refill
  is timer-driven and **pauses the experiment in the middle of a Z-plane**
  (the scan job is suspended and resumed). At 4 of 4 refills at **D7** the
  frame was cut mid-scan and every later plane and position came back
  completely dark at the **correct** Z (DF failing) — no light through the
  objective, the refill (5 s suction, 2.5 s feed, 2 s wait) left no working
  bridge. 14 refills at other wells were harmless; a manual *Remove + Create*
  water fixed it each time, a ZEN restart did not. Position-dependence is a
  correlation without a control (runs were restarted at D7); cause of the
  water loss unknown. ZEN never errors out — it keeps acquiring black images.
  No fluid-level sensor on this CD7 (`MTBFluidLevelFreshImmersion` fails to
  be created). Full analysis: `Mike_Projects/20261028_Ati_CD7_Issues/
  04_D7_refill_breaks_immersion.md` (issue 3). The D7 refill at 04:42 is the
  likely trigger of the New-03 failures from D1 on (issue 2).
  Afterwards New-03's DF, where it succeeded, locked a constant
  **+138.6 µm (sd 1.3)** above the real surface (measured against New-04) —
  the mirror of the −163 µm underside lock; both are ±151 µm (one optical
  glass thickness) if the plate sat ~12 µm lower overnight (inferred, not
  measured). A ghost reflex is the leading guess for the + lock; unconfirmed.
  Lessons: a refill mid-stack can silently ruin the rest of a run; a dark
  stack at a plausible Z after a refill means "check the water", not focus;
  stop a run on an all-zero plane or repeated DF failures.

- **The re-immersion settings are NOT reachable through the ZEN-API (checked
  2026-11-09, ZEN 3.13.109, gateway 3.6.25262).** Neither the 2025.10.1 stubs
  and docs nor the live gateway have an immersion or options service. gRPC
  reflection (`grpc.reflection.v1alpha`; v1 is not served) lists 16 `zen_api`
  services, none of which can read or set them. How ZEN handles them instead:
  - They are per ZEN user in
    `C:\ProgramData\Carl Zeiss\ZEN\Users\<user>\DefaultWorkspace\Options.config`
    (XML, `<OptionEntry Name=".." Value=".." />`). The keys ZEN uses:
    `AutoImmersionRefillInterval` (min), `AutoImmersionRefillTravelDistance`
    (mm), `IsAutoImmersionRefillGlobalEnabled`,
    `IsAutoImmersionRefillForExperimentEnabled`,
    `IsAutoImmersionRefillTravelDistanceEnabled`, `IsAutoImmersionEnabled`,
    and the pump times (`AutoImmersionRemovalPumpDuration` 5000 ms,
    `AutoImmersionFeedPumpDuration` 2500, `AutoImmersionWaitingDuration`
    2000). `AutoImmersionRefillTime` (45) and
    `IsAutoImmersionRefillTimeEnabled` also show up in some files but ZEN does
    not send them on, so they look like leftovers. There is no flag to turn
    off only the time-based refill.
  - At startup, ZEN.exe sends them to ZENService in an internal
    `SetServiceOptions` request (ZENService log, e.g. 2026-11-09 10:04:06).
    That is ZEN's own channel, not the gateway, and it is not documented. The
    refiller then logs `ImmersionTimeInterval = 7200000 ms` (= 120 min).
  - The logged-in ZEN user can be read from `ZEN.log.xml`
    (`SaveToFile(): Successfully written options config file
    '...\Users\<user>\...'`). ZEN also writes this file during the session
    (2026-11-09: 10:05 and 10:56), so a hand edit while ZEN is running gets
    overwritten.
  - Users differ: most have 120 min / 200 mm (the defaults). **Atreya has
    10 min / 40 mm** (file dated 2026-10-30, after the New-03/04 incidents).
  Answers to the TODO: **reading** works without the API. Parse the
  logged-in user's `Options.config` (or, more reliably, the last
  `SetServiceOptions` payload in the ZENService log, which is what the service
  actually uses, but logs rotate after ~5 days). Both are read by
  `zeiss_paths.zen_auto_immersion_settings()` (newest log files first, at
  most 40, ~0.2 s), and `preflight()` logs one line with both and a
  `MISMATCH` flag when they differ. It only logs and never aborts a run.
  **Changing** only works in
  the ZEN UI or by editing the file while ZEN is closed (untested,
  unsupported). Not tested: whether ZEN sends `SetServiceOptions` again when
  the option is changed in the UI during a session. Caution for long 50x
  runs: a shorter interval means more refills, and a refill can break the
  bridge (entry above).

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
  - **A rerun into the same folder gets `<name>_000001.czi`, `_000002`, …** That is
    SmartMic's `_unique_czi_name`, NOT ZEN: it logs "File X.czi already exists.
    Using X_000001.czi instead." (Marc_SM_003 shakedown reruns, 2026-11-02; the
    project notes first credited ZEN). Nothing is overwritten, but the file is not
    under the requested name: always use the `exp_result_path` the run returns,
    never rebuild the path from `custom_filename`.

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

**Where an image was acquired: the CZI pixel frame IS the stage frame**
(verified 2026-09-30 on the CD7, details in ia_common README):
`stage_xy = global_pixel_coordinate × pixel_size`, with no offset.
- 5x OV tiles: within 0.2 µm of each tile's recorded stage position.
- 40x camera, LSM and Airyscan images: the pixel-box centre equals the
  commanded position within 0.1 µm.

Exceptions and caveats:
- **Processed Airyscan files** reset the pixel box to (0, 0) and crop a
  12-px border. Use the recorded scene centre there (the crop is symmetric).
- **Not yet checked:** asymmetric camera ROIs and LSM scan offset / rotation
  (`LaserScanInfo/SampleOffsetX/Y`, `SampleRotation`).
- **Image-content offset:** the channel attribute `MM.PixelShift` (40x
  camera: (−12.5, +18.0) µm; 5x OV: (−50.1, +71.8) µm; LSM: 0) may be ZEN's
  per-magnification image-shift calibration. It is unexplained so far; see
  TODO "INVESTIGATE MM.PixelShift".

**Authored vs injected position:** `_002` has an authored P1 (added in the ZEN UI, so
the experiment is in use-positions mode) and navigates. An experiment with an empty
`<SingleTileRegions/>` that we merely inject a region into (e.g. `_001` via
`add_single_tile_region`) may NOT be honoured for navigation — set the position by
editing an **already-authored** position instead (`set_position` on `_002`). Verify
per experiment by image content.

**`FocusZStackMode`, not `IsCenterMode`, decides whether a z-stack's First/Last
are relative or absolute** (found in Marc_SM_003, 2026-11-02; safety-relevant).
- The field is `<FocusSetup><FocusStrategy><FocusZStackMode>`, i.e. part of the
  focus strategy, not of the `ZStackSetup`.
- `CurrentZ` → First/Last are **offsets** from the focus at the position (the
  region `<Z>` when there is a used region). Seen: Marc_SM_003's 2.5x BF sweep,
  Marc_SM_002's CENTER-mode Airyscan stack.
- `FixedZ` → First/Last are **absolute Z**, also when `IsCenterMode` is true;
  the region `<Z>` does not shift the stack.
- What happened: a FixedZ + CENTER-mode LSM stack given relative ±200 µm ran at
  absolute Z −200 … +200 µm, about 2.9 mm below the sample. That is the safe
  direction on this inverted scope, but outside the project's Z guard. The
  user's own FixedZ file (absolute 2856 … 3256 µm) ran exactly there.
- The old `MS_czexp_editor` docstrings explained the stack centre via
  `IsCenterMode` / the region `<Z>` only, which misled the project code.
- **Library (2026-11-09):** `MS_czexp_editor.get_focus_z_stack_mode` reads it,
  `require_focus_z_stack_mode` refuses a mismatch, and
  `set_zstack_range(..., expect_mode="CurrentZ" | "FixedZ")` checks before
  writing. `expect_mode=None` still writes unchecked, so existing project callers
  keep working; new code should always pass it. A missing field (no focus
  strategy saved) counts as unknown and is refused.
- A software-autofocus strategy embeds a complete experiment with its own
  `FocusZStackMode` and `ZStackSetup`. Those are ignored, by both the reader and
  `find_zstack_setup` (which used to fall back to the SWAF's stack when the
  experiment had none of its own).
- **Builder (2026-11-09, promoted from Marc_SM_003):**
  `MS_czexp_editor.fit_lsm_stack_to_box(base, out, center_m, size_m,
  expect_mode=…, xy_margin_m=…, z_margin_m=…)` writes an LSM z-stack + square
  crop fitted to one object's box. `expect_mode` is required: the caller says
  which kind of Z it handles and a base in the other mode is refused. Its
  `z_reference` says what the stack is relative to: `absolute` (FixedZ),
  `region_z` (CurrentZ with a position region, set to the centre) or
  `current_z` (CurrentZ without one: move Z to the centre before the run).
  Marc_SM_003's finals were byte-identical to its old code (36 cases).
- **The two older copies of this builder are not switched** (own repos):
  - Marc_SM `make_spheroid_dv` always writes ABSOLUTE First/Last, with a
    comment that ZEN centres a CENTER-mode stack on the region `<Z>` (the old,
    wrong belief). It is right only because its DV bases are FixedZ (checked
    2026-11-09); a CurrentZ base would run millimetres off.
  - HD_Nuclei_from_slide `make_image_nuclei`: the same steps, no mode check.

**Method 1 needs Tiles OFF: Tiles on with no tile region makes ZEN refuse the
run** (found in Marc_SM_003, 2026-11-02, shakedown step 4).
- The final LSM base had been saved with Tiles switched on
  (`<TilesSetup IsActivated="true">`) but with no `<TileRegion>` or
  `<SingleTileRegion>`. `run_experiment_from_path` failed with "ABORTED: Failed
  to start experiment". Nothing moved and no image was written.
- After the user re-saved it with Tiles off, it acquired at the current stage
  position (method 1 above), as intended.
- **Library (2026-11-09):** `MS_czexp_editor.require_runnable_tiles(root, path)`
  refuses this before the run (`tiles_activated` reads the flag). It accepts
  any region and doesn't check `IsUsedForAcquisition`; whether ZEN also refuses
  a file whose regions are all unused is untested.
- Same promotion: `require_no_focus_search(root, path, reason=…)`, for runs that
  set Z themselves. It refuses a focus strategy with a `SearchAction`
  (DefiniteFocus, SoftwareAutofocus) but allows one without, e.g.
  FollowSurface + SearchAction None, which only uses the stored region Z (how
  Marc_SM_003's re-aimed OV gets its focus). Stricter than `has_focus_strategy`,
  which refuses any strategy.
- Both ignore the copy inside an embedded software-autofocus experiment.

**Re-aiming a tile region at another well: rewrite its container link too**
(Marc_SM_003, 2026-11-02; promoted 2026-11-09).
- A `<TileRegion>` is tied to its well three ways: `<CenterPosition>` (+ `<Z>`),
  its `Name` (which becomes the CZI scene name), and `<TemplateShapeId>`, ZEN's
  link to the carrier container. Marc_SM_003 makes each well's OV from one
  region authored for B2 and rewrites all three. On the scope the re-aimed
  region imaged the right well, under the right scene name.
- Why the link: the base has `<IsContainerAdaptionEnabled>true`, so a region
  still linked to B2 might be moved back to B2 by ZEN. **Not observed** (the
  link was always rewritten); a precaution, untested either way.
- `TemplateShapeId` format: "row-column", 1-based (B2 → `2-2`, H12 → `8-12`).
  Seen on 96- and 384-well carriers only.
- **Take the ID from ZEN, don't compute it from the name.** The name is a label:
  a 1536-well plate (32 × 48) has rows past Z (normally AA–AF; how ZEN names
  them is unconfirmed, no 1536 experiment on this machine), and carrier
  templates can switch or invert the row/column labels
  (`AreAxisAnnotationsSwitched`, `Is{Row,Column}AnnotationDirectionInverted`).
  Marc_SM_003's first version worked the ID out from a single-letter row; it
  refused two-letter rows but would have mapped lowercase ones to the wrong well.
- **Library:** `MS_czexp_editor.get_tile_region_shape_ids(root)` reads
  `{name: TemplateShapeId}` from an experiment ZEN laid out with a region per
  well (Marc_SM_003: its 4-line scan, also its source of well centres).
  `retarget_tile_region(root, name, x_m, y_m, z_m=, shape_id=)` rewrites all
  three and refuses to leave a stale link (a region that has a TemplateShapeId
  needs `shape_id`). Its output was byte-identical to Marc_SM_003's old code
  for all 48 wells.

---

## Multi-slide carriers: containers = ZEN "Scan Positions" (3x slide insert, 2026-09-29)

Learned on the CD7 with carrier `Insert - 3x Slide - Chamber Slide 8x -
432332-9110-000` (1 row × 3 columns, one 8-well ibidi slide per position;
project Marc_SM_002). ZEN's UI calls the three slide positions **Scan
Positions**; the API calls them **containers** (`SampleCarrierService`,
`lm.hardware.v1`, wrapped in `MS_zenapi_sample_carrier.py` / LoA
`get_current_container`, `move_to_container`).

- **Stage XY is relative to the selected container.** The stage's software
  limits are X 26–26026 µm, Y −147–75852 µm: one slide, not the carrier. OV
  experiments authored for slide 1, 2 and 3 have **identical** tile-region
  coordinates (A1 at (7300, 19100) µm on every slide), and the `.czexp` stores
  **no** container (`<Containers />`, `MultiCarrierSetup IsActivated="false"`,
  `CarrierStagePosition Unknown`). Which slide is imaged is decided by the
  container selected in ZEN when the experiment runs, not by the file.
- **`MoveToContainer(row, col)` physically switches slides** (0-based;
  slides 1/2/3 = (0,0)/(0,1)/(0,2)). It takes about 4 s, and ZEN's UI shows the
  switch. Afterwards the stage XY reads **(13000, 38000) µm**, the slide centre
  in container coordinates, on every container. Subsequent
  `move_stage_to_new_xy_position` calls and `RunExperiment` tile regions run in
  that container's frame. Verified by image content: the OVs of the three
  containers share ≤ 1.1 % of nuclei, and each OV mosaic sat ≤ 0.3 µm from its
  tile region.
- **`GetCurrentContainer` does not work here:** it always answers NOT_FOUND
  ("Stage is not positioned in any of the containers"), even right after
  `MoveToContainer`. `get_current_container()` returns None for that. Don't
  use it as a check. Verify the slide from the images instead: compare
  nuclei with an earlier OV (Marc_SM_002 `check_not_same_slide` /
  `register_ov`).
- **`MoveToContainer` retracts Z, then puts the OLD Z back** (watched in the
  ZEN UI, 2026-10-14). During the switch ZEN drives Z down to about
  −1000 µm, moves the stage, then returns Z to the value it had when the
  call was made: from 0.000 it came back at 4.59 µm, both directions, stable
  afterwards. (So the "drive floors near −293 µm" above is the limit for our
  own moves; ZEN's retract goes deeper.) The 2026-09-29 note "Z stayed at the
  last DF surface" was this round trip. Consequences:
  - The switch itself is collision-safe, but the restored Z is the old
    slide's height on the new slide. Surfaces differ between slides by
    > 100 µm, so with a short-working-distance objective that Z can be past
    focus. Hence the library's `move_to_container` lowers Z to 0 first,
    which leaves Z near 0 (4.59 µm) on the new slide: re-focus there.
  - With the 50x immersion objective the retract presumably breaks the water
    bridge. **Unverified:** ZEN may re-apply immersion automatically after
    the switch (CD7 auto-immersion), see TODO. Until that is checked,
    `move_to_container` refuses with the immersion objective active. Our
    own Z=0 lowering would break the bridge anyway.
  - **What the ZEN service logs say about auto-immersion (read 2026-10-27,
    still no answer for the container switch).** The API has no immersion
    service (only the read-only `immersion_type` of an objective), and
    `MoveToContainer` is documented without any word on Z or water. ZEN's
    auto-immersion lives in the hardware layer
    (`ImmersionAdapterCastor`, `ImmersionRefiller`, `ImmersionAdapterBase` in
    `C:\ProgramData\Carl Zeiss\Logging\ZENService.log.xml*`): it reacts to
    objective-changer, stage and focus property changes and tracks
    `StageTravelledDistanceFromLastImmersionPoint`. On this scope
    `IsAutoImmersionRefillGlobalEnabled = True`. The refiller timer stops
    with a dry objective ("Stopped periodic timer") and runs with the 50x
    and water applied: `ImmersionTimeInterval = 7200000 ms` (2 h). So ZEN can
    renew the water by itself, by time or by travelled distance, without any
    API call. Whether a container switch triggers it is **not** shown: the
    logs only go back to 2026-10-23, the 2026-10-14 check (5x) has rotated
    out, no container switch is in them, and gRPC calls are not logged by
    name. Needs the ZEN UI check (TODO).

  **Library `move_to_container` (since 2026-10-14):** refuses with the
  immersion objective active, otherwise lowers Z to 0, switches, and settles.
  Given a `DefiniteFocusTracker`, it resets it (the last surface belongs to
  the previous slide). **Verified on the scope 2026-10-14**
  (`sandbox/MS_hw_check_move_to_container.py`; carrier `Insert - 3x Slide -
  432332-9110-000`, empty slides, 5x). That is a different ZEN carrier from
  Marc_SM_002's: the same 3x insert, but defined for plain 76 × 26 mm slides,
  where `Insert - 3x Slide - Chamber Slide 8x - 432332-9110-000` is defined
  for ibidi 8-well chamber slides. Slide 1 → 2 and back, from Z 300 µm.
  Each time Z was 0.000 µm before the switch was issued. The switch took
  ~5 s including the 1 s settle. Afterwards XY was (12999.99, 38000.00) µm
  and Z 4.59 µm. The operator confirmed the order in ZEN: Z down, then the
  stage, then Z back up.
- **A re-seated slide moves AND rotates.** Slide 1, taken out and put back
  overnight: (+166, +6) µm and **−0.175°**, which is ≈ 10 µm at the edge of
  a 7 mm well. A shift-only match of OV nuclei found 22 %; a rigid
  (rotation + shift) fit found 91 %, with a 2 µm median residual. Re-finding
  cells after re-seating needs a rigid fit. Don't reuse old coordinates
  directly: the error is larger than a nucleus.
- **The glass surface is not level (DF surface Z):** ≈ +108 µm from well
  row A to row B (12.1 mm in Y) on slides 2 and 3, and +16–30 µm from
  column 1 to 2 (10.9 mm in X). Slide 1: 1350 µm at A1 → 1505 µm at C1. The
  whole insert is tilted. DF copes (the "start 100 µm below the last
  surface" approach found every surface in the test; `DefiniteFocusTracker`
  adds a full-range fallback for a surface below the adaptive start), but
  don't assume one Z per slide.

## What a CZI / .czexp records about the sample carrier (2026-11-09)

Read by `MS_Helper_function.get_sample_carrier_from_czi` / `_czexp`, checked
by `check_sample_carrier`. Surveyed on every `.czexp`/`.czi` on this machine
(tests fixtures, all projects, Marc_SM_002 on `F:\UserData\mike`).

- **Where:** `…/SampleHolder/Template` (`Name` attribute plus geometry:
  `ShapeRows`/`ShapeColumns`, `ShapeWidth`/`ShapeHeight`,
  `ShapeDistanceX`/`Y`, `BoundsSize`, `AnchorPoint`, `BottomThickness`, all
  **µm**; e.g. a 384 plate: pitch 4500, bounds 127800,85500). SWAF/stitching
  experiments carry a second copy under `AdditionalSettings/
  ScanAreaSampleHolderInfo`, named with a `.czsht` suffix
  (`Multichamber 384.czsht`), and nested `SampleHolder` blocks without a
  Template. Use the outermost `SampleHolder/Template`.
- **A plate** (`InsertType` `Default`): the Template IS the ZEN carrier,
  e.g. `Marc_96W_U-Bottom`. Its name should equal the API's
  `get_sample_carrier_name()` (not yet confirmed on the scope; TODO).
- **Slides in an insert** (`InsertType` `PartOfInsert`): the Template is the
  ONE SLIDE, e.g. `Chamber Slide 8x - 76 x 26 mm - Vertical` (Marc_SM_002)
  or `Slide 76 x 26 mm - Vertical` (HD_Nuclei, Slide_Search).
  `PartOfInsertName` is empty, so the ZEN carrier (`Insert - 3x Slide -
  Chamber Slide 8x - 432332-9110-000`, what a project's
  `EXPECTED_SAMPLE_CARRIER` holds) is **not in the file**, and neither is
  the slot (see the section above: `<Containers />`). So a file's carrier
  name never equals the API's for an insert. `check_sample_carrier` returns
  "can't check" there unless given the expected slide name, and never a
  mismatch. Which slide was imaged can only be checked from the images.
- Neither file records a plate identity (barcode, holder number).

## Tray load / eject: LiveScanService (2026-11-09)

`zen_api.lm.live_scan.v1.LiveScanService` has `LoadTrayAndPrescan`,
`EjectTray` (both take **no arguments**), `GetConfiguration` and
`SetConfiguration`. What the prescan does on load is set only through the
configuration. `MS_zenapi_live_scan.py` wraps `GetConfiguration`,
`EjectTray` and `LoadTrayAndPrescan`. It does not wrap `SetConfiguration`
yet. `preflight()` logs the configuration on one line: template,
material/RI, and the prescan steps that are on and off. The line also shows a
template / ZEN carrier mismatch. The line is for the log only. It never stops
a run.

- **`GetConfiguration` works on this gateway** (2026-11-09, 6-well plate in
  ZEN). Returned: `sample_holder_template='Multiwell 6 - 250um.czsht'`,
  `measure_bottom_thickness=True`, `use_right_barcode=True`, every other flag
  (detection, material, overview, barcodes, calibration) `False`,
  `refractive_index=1.594`, `material='Polystyrene'`.
- **The template is the ZEN carrier name plus `.czsht`:**
  `SampleCarrierService` reported `'Multiwell 6 - 250um'` at the same time,
  with the same material/RI. (The `.czsht` suffix is the same convention as
  `ScanAreaSampleHolderInfo` in a `.czexp`, see the section above.) Not
  checked: whether `SetConfiguration` with another template changes the ZEN
  carrier.
- `SampleCarrierService` thickness was `176.178947…`, not the 250 µm in the
  name. Expected: the plate was glass, measured at load as 168 µm, and the
  material had been switched to Polystyrene for the DF material test. ZEN then
  rescales to keep the optical thickness: 168 × 1.594/1.52 = 176.18 µm (see
  "Dry objectives: DF finds the plate UNDERSIDE" above). So the reported
  thickness is the measured one, as rescaled for the current material, not
  the template's. A second read later the same day showed Glass / 1.52 again.

### What ZEN does on eject and on load (from the ZEN logs, 2026-11-09)

Source: 4 ejects and loads from the ZEN UI on 2026-11-09, in `ZEN.log.xml` and
`ZENService.log.xml.10`. The UI button logs
`LiveScanManager.EjectTray()`, the same name as the API RPC. Thus the API
RPC probably runs the same sequence. This is not confirmed yet.

**Eject** (15:07:54, approximately 11 s):
1. ZEN stops DF (`Stop Definite Focus`).
2. ZEN records the last scan position (`lastScannerPositionCoordinates`, X, Y and Z).
3. ZEN retracts Z. Z went from 1153.6 µm to −1099.3 µm in approximately 1.1 s
   (`MtbLiveCellScanner` focus position `TubelensChange`).
4. Then the stage moves to the load position (`StagePosition = Load`).

Thus the eject does not need a Z move of our own. ZEN retracts Z before the stage moves.

**Load and prescan** (15:08:24, approximately 21 s, all prescan steps off):
1. ZEN starts the job `Load Sample Carrier` (`ExecuteTrayScan`). The log shows the
   prescan flags: `doRecognition`, `doReadBarcodes`, `doOverview`,
   `detectThickness`, `detectMaterial`.
2. **ZEN changes the objective from position 1 (20x/0.7) to position 2 (5x/0.35).**
   For the change, Z goes to −8317 µm. Then Z goes to −1099 µm.
3. ZEN sets the DF thickness to 0 and moves the stage to `ScanWellPlate`.
4. ZEN runs `FindSurfaceForAllScanPositions` (FindFocus).
5. At the end, Z is at 7.0 µm and the DF thickness is 168 µm again.
   **The objective stays at position 2 (5x).** ZEN does not change it back.

Consequences:
- After a load, set the objective again before you acquire.
- Do not start DF while a load runs (see "Dry objectives: DF finds the plate UNDERSIDE").
- With thickness measurement, barcodes or detection on, the load takes longer.
  Use a long timeout.

### Eject with the 50x immersion objective (decided 2026-11-09)

The eject also works with the 50x (Mike, 2026-11-09). The safe procedure is:
change to the 5x (air) first, then eject. `MS_CD7_API_LoA.eject_tray()` does
this by default when the 50x is active. Use `switch_to_dry_objective=False` to
eject directly on the 50x.

### First eject and load over the API (2026-11-09, 16:05)

Start state: idle, 20x/0.7 (objective 1) with 2x tubelens, Z 1169.2 µm,
glass 6-well plate. Calls: `MS_CD7_API_LoA.eject_tray()`, then
`load_tray_and_prescan()`. Prescan: only `measure_bottom_thickness` on.

**Both RPCs block until ZEN is ready.** The wrappers do not need to wait.
- `EjectTray` returned after 11.0 s. The tray reached `Load` at 16:06:09.57.
  The RPC returned 0.08 s later. The sequence was the same as from the UI:
  stop DF, record the last position, Z to −1099.3 µm, stage to `Load`.
- `LoadTrayAndPrescan` returned after 46.4 s. That was 0.2 s after the last
  FindFocus of the prescan.

**The load uses the live-scan configuration.** The log shows
`detectThickness = True`, the same as `measure_bottom_thickness`. All other
flags were off, also in the configuration.

**Load sequence with thickness measurement:**
1. `FindSurfaceForAllScanPositions`. ZEN stops and resets DF. Then the stage
   goes to `ScanWellPlate` (approximately 11 s).
2. The stage goes to a detection point. This time it was the XY from before the
   eject (14760, 20380). The UI load at 15:08 ended at a different XY. Thus
   ZEN does not always use the last XY.
3. FindFocus with thickness 0 found Z 1082.9 µm (the underside).
4. `CoverGlassDetectionExperiment` ran for approximately 13 s. It wrote
   `F:\UserData\mike\CoverGlassDetectionExperiment.czi`.
5. A second FindFocus with the measured thickness. Z was 1172.4 µm at the end.
   That is 89.6 µm above the underside, for 168 µm of glass.

**The RPC returns the measurement as a notification:**
`Cover Glass Thickness Detection: Cover glass detection succeeded: Thickness: 168 µm
Skirt: 1045 µm Objective correction rings adjusted.` After the load,
`SampleCarrierService` showed thickness 168.0, skirt 1045.0, Glass, RI 1.52.

**After the load the objective was 2 (5x).** The optovar stayed at 1 (2x).
The stage was at the XY from before the eject.

Not tested yet: an API eject on the 50x (with and without the change to the 5x).
`SetConfiguration` is not wrapped yet (see `TODO.md`).

---

## Decisions (persistent)

### Writing style: ASD-STE100 Simplified Technical English (decided 2026-11-09)

Write new text in this file in ASD-STE100 Simplified Technical English (STE). Use the same style in `TODO.md`, in the project notes and in reports. The target is 90 % or more. Technical names, code names, file names, units and quoted log lines do not count.

Rules to follow:
- Write one topic in each sentence.
- Use a maximum of 20 words in an instruction and 25 words in a description.
- Use a maximum of six sentences in a paragraph.
- Use the active voice. Use the simple tenses: present, past and future.
- Give instructions as commands, for example "Do not move Z to 0 on the 50x objective."
- Use approved words. For example, write "use" (not "utilize"), "make sure" (not "ensure"), "approximately" (not "~" or "about"), and "must" or "can" (not "should").
- Do not use contractions such as "don't" or "it's".
- Write technical names, code names and log lines in `code` font.

Do not rewrite old entries only to change their style. When you change an old entry, change its style too.
Reason: these notes record hardware facts for different readers, some of whom do not have English as their first language. Short and clear sentences decrease errors. The summary PDF of `projects/DF_Exploration` uses the same style.

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

**Per-run version record — promoted from Marc_SM_003 (2026-11-09):** the
preflight line is for a human reading the log; a dataset also needs a
machine-readable record of the exact code. `MS_Helper_function.write_run_info
(log_file, log, repos={...}, extra={...})` writes `run_info_<ts>.json` beside
`setup_run_logger`'s `run_<ts>.log`: SmartMic's FULL commit + uncommitted files,
the same for every repo passed in `repos` (the project's own repo, its ia
repos), and the four version identities above. Uncommitted files are logged as
a WARNING per repo, so a run on modified code shows in the run log too. The
per-repo state comes from `zeiss_paths.git_state(path)`, which also returns the
checkout `root` git reported on: a folder that is not its own repo reports the
ENCLOSING repo (e.g. a project folder under `SmartMic/projects` without its own
`.git` would report SmartMic's commit), and `root` makes that visible. Marc_SM_003
used it for plates 1–4 with the repos at the top level of the JSON; the library
version puts them under `"repos"`.

### Promoted from Marc_SM_002 (decided 2026-09-29, implemented 2026-10-14)

The Marc_SM_002 run produced project code that every pipeline needs. It now
lives in the library. Project copies are unchanged and switch over when
convenient (Marc_SM_002 only after its STZ run is finished).

| Library | Replaces (project) |
|---|---|
| LoA `move_to_container`: immersion refusal, Z=0 first, settle, optional tracker reset | Marc_SM_002 `goto_container` |
| LoA `move_focus_to_new_z_position(..., tolerance_m)` and `move_stage_to_new_xy_position(..., tolerance_m)` | Marc_SM_002 `move_z` (no XY version existed) |
| LoA `set_optics_verified` | HD_Nuclei / Marc_SM_002 `ensure_optics` |
| LoA `DefiniteFocusTracker` + `DefiniteFocusFault` | Marc_SM_002 `FocusState`; the inline adaptive start in the PoC / HD_Nuclei |
| `MS_czexp_editor.get_tile_region_centers` | Marc_SM_002 `ov_tile_regions`; Slide_Search's own parsing |
| `MS_image_analysis.run_analysis_json` | Marc_SM_002 `analyse` and the same pattern in every project |
| `MS_Helper_function.append_jsonl` / `read_jsonl` | Marc_SM_002's cells log + `read_cells_log` |

Design choices worth knowing:
- **Backward compatible.** New parameters default to the old behaviour, so
  projects running against the live library are unaffected until they opt in.
- **Tolerant moves handle only `GRPCError`** (what ZEN raises, e.g.
  `(<Status.INTERNAL: 13>, 'Requested position was not reached.', None)`).
  The immersion guard now raises its own `ImmersionObjectiveActive` (still a
  `RuntimeError`), so it can never be mistaken for a hardware error and
  retried. After an accepted XY error, the stage settle wait is still applied.
- **The tracker never runs a full-range DF search with the immersion objective
  active.** That search starts at −300 µm, which drops the bridge (and fails
  at 50x anyway). With no known surface it raises (seed it with
  `surface_z_m`); the fallback is skipped. `reset()` forgets only the
  surface, not the failure count: a slide change does not fix a DF fault.
  `move_to_container` resets the tracker *before* the move, since forgetting
  is always safe. Use one tracker per objective, or `reset()` after an
  objective change: surfaces differ between objectives.
- **`run_analysis_json` ignores a stale result** (mtime older than the
  launch). In Marc_SM_002 a failed attempt reuses the next cell's tag, so an
  old file with the same name can exist.
- **`append_jsonl` starts on a new line if the file doesn't end in one.**
  Otherwise the first record after a crash gets glued onto the cut-off
  fragment and is lost too. Each record is fsync'ed.

**Decided NOT to promote:**
- OV re-registration (rigid slide matching): a one-off rescue for re-seating
  a slide mid-experiment, not normally needed.
- `acquire`: too small, and the error handling is project-specific.
- `RunningMedian`.
- The per-project workflow and resume rules.
- Image-analysis helpers: these go to `Image_Analysis/ia_common`, not here
  (see "Image-analysis repos").

---

## Code review round (2026-11-10)

A review of the tracked library found 52 items (the local `fableTodo.MD`).
Items 1, 2, 7, 8, 15 and most of 16 to 42 are done. The offline suite passes
(397 tests). None of it is tested on the scope yet. The PoC was not changed
(it is outdated; an update is planned).

### Where the code is now (old name → new name)

Old entries in this file use the old names. Do not rewrite them; use this table.

| Old | New |
|-----|-----|
| `MS_Helper_function.setup_run_logger`, `write_run_info`, `append_jsonl`, `read_jsonl`, `DEFAULT_EXPERIMENT_OUTPUT_FOLDER` | `MS_run_log` |
| `MS_Helper_function.load_positions_from_czexp`, `get_sample_carrier_from_czi` / `_czexp`, `check_sample_carrier`, `get_focus_position_from_czi`, `get_zstack_z_range`, `get_stage_position_from_czi`, `get_xy_position_from_czi` | `MS_metadata` |
| `MS_Helper_function.compute_focus_score`, `signal_level_from_czi`, `is_czi_effectively_empty` | `MS_image_metrics` |
| `zeiss_paths.zen_app_version`, `zen_df_bottom`, `zen_auto_immersion_settings`, `format_auto_immersion_settings`, `zen_user_options_path`, `zen_service_immersion_options` | `MS_zen_logs` |
| `MS_zenapi_experiment_methods.check_experiment_api` | `MS_zenapi_experiment_smoke` |
| `ms.run_experiment(..., do_snap_and_live=True)` | removed; call the smoke test directly |

The old names still work: `MS_Helper_function` re-exports its three modules, and
`zeiss_paths.<name>` resolves the names of `MS_zen_logs`. The exception is
`check_experiment_api`: import it from the new module.
`zeiss_paths` keeps the path bootstrap, `zen_api_version`,
`zen_api_gateway_version`, `smartmic_version` and `git_state`.

### Changes of behaviour

- **Messages go to the run log.** The LoA and the helper modules do not use
  `print`. Objective-change timeouts and retries are now in `run_*.log`.
- **The position checks refuse `NaN`, `±inf` and `bool`.** A comparison with `NaN`
  is always false, so the old range test let `NaN` through to `MoveTo`.
- **`load_positions_from_czexp` raises** for a used position with a missing or
  unreadable `<X>` / `<Y>`. Before, the value became 0.0. A missing `<Z>` is
  still 0.0.
- **`set_stage_motion` raises** for a percent that is not in [0, 100].
- **A bad argument is not a failed search.** `run_definite_focus_find_surface`
  and `set_objective_set_optovar_sync` let `ValueError` / `TypeError` through.
- **The czexp locators ignore an embedded autofocus experiment.**
  `find_lsm_detector`, `find_tile_regions`, `find_stitch_regions`,
  `find_processing_steps`, `is_stitching_configured`, `get_focus_strategy`,
  `add_single_tile_region` and `clear_single_tile_regions` now use `_iter_own`.
  In 405 local `.czexp` files the first region, detector and focus set-up were
  the same with the old and the new locator. `is_stitching_configured` was the
  exception: in 70 files of the ZEN experiment folder it read the processing
  set-up of the embedded SWAF experiment.
- **`set_tile_region_center` raises** if `z_m` is given and the region has no
  `<Z>`. No local file has such a region (995 regions examined).
- **`fit_lsm_crop` reports** `frame_clamped`, `frame_size_requested` and
  `fov_exceeds_max`. It does not refuse: what ZEN does with a field larger than
  `ScaledMaxImageSize` is not known.
- **`save_czexp` writes to `<name>.tmp`, then moves it** into place.
- **`compute_focus_score` uses interior pixels only.** `np.roll` compared the
  first row with the last row. Scores change a little: do not compare a new score
  with a score from before 2026-11-10.
- **`get_current_objective_and_optovar` sends two RPCs, not four.** It does not
  read the inventory.
- **`config.ini` and the TLS context are cached** for each file and modification
  time (`MS_zenapi_helpers._connection_settings`). A new channel is still opened
  for each call.
- **`SMARTMIC_OUTPUT_ROOT`** overrides `DEFAULT_EXPERIMENT_OUTPUT_FOLDER`.
  `SMARTMIC_ZEN_EXPERIMENT_FOLDER` overrides the experiment folder of the smoke
  test.

### Rule for new code: a failure raises (proposal, Mike to confirm)

The library reports a failure in four ways today: an exception (moves), a
`(False, message, attempts)` tuple (DF), `None` (RecallFocus, SWAF) and `False`
(optics, `preflight`). A caller that does not examine the result continues with
no error. New functions must raise. Do not change the old functions without the
projects that call them.

### Not done, and why

- **Units of return values (item 19).** `run_swaf` and `run_definite_focus_recall`
  return µm; the moves and the tracker use metres. A change breaks the projects.
  Do it together with them.
- **Loaded experiments stay in ZEN (item 27).** This needs a measurement on a
  long run.
- **Region id (item 41).** `add_single_tile_region` makes a region only if the
  experiment has none, so the fixed id cannot collide in one file.
- **Names (item 42).** This is a decision, not a fix.
- **`get_focus_position_from_czi` (item 28)** still scans the first 8 MB. It now
  also reads exponent notation.

---

## Code review fixes (2026-07-15)

A full-codebase review (8 finder angles, each verified against the actual
source) found 10 issues; 9 were added to `TODO.md` and 8 were fixed same-day
(offline suite: 129 passed after). One deferred — see `TODO.md`.

- **PoC silently proceeded after a failed DefiniteFocus.**
  `MS_SmartMic_PoC.py` unpacked `run_definite_focus_find_surface`'s
  `(success, message, attempts)` as `_, _, df_attempts` — only the retry count
  was checked, never success. A failed DF still had its Z read back and stored
  as `last_surface_z_m` (corrupting the next position's adaptive start), and
  the pipeline proceeded into SWAF/acquisition out of focus. Fixed at both
  call sites (overview + per-nucleus): check `success`, skip the position (or
  raise into the existing per-nucleus `except`) on failure.

- **The DF wrapper's `False` branch was dead code.**
  `MS_zenapi_focus.definite_focus_find_surface` only ever returns
  `(True, attempts)` or raises (after exhausting `max_retries`) — it never
  returns `(False, attempts)`. `MS_CD7_API_LoA.run_definite_focus_find_surface`'s
  `if success: ... else: return False, ...` therefore had an unreachable
  branch, and its `except` hardcoded `attempts_used = max_retries` instead of
  the real count. This contract mismatch (plus a unit test mocking the
  impossible `(False, 3)` shape) is what let the PoC bug above go unnoticed.
  Fixed: removed the dead branch; `definite_focus_find_surface` now attaches
  the real `attempts_used` to the exception it raises, and the wrapper reads
  it (falling back to `max_retries` only if absent); the test now mocks a
  raised exception with `.attempts_used` set instead of a clean `False` return.

- **Import-time `mkdir()` hardcoded the `F:` drive.**
  `MS_zenapi_experiment_methods.py` had a module-level
  `image_folder = DEFAULT_EXPERIMENT_OUTPUT_FOLDER; image_folder.mkdir(...)`
  that ran on every import (transitively via `MS_CD7_API_LoA`) — turned out to
  be dead weight: no real function actually read that global (they all go
  through `_resolve_image_folder`, which creates its own folder lazily), so on
  any machine without `F:` mapped this would break import/test-collection for
  no reason. Deleted the unused global + its mkdir + the stale disabled debug
  snippet that referenced it.

- **`zeiss_paths.py` picked the wrong `zen_api` package once a version had two
  digits.** `sorted()` on `zen_api-<version>/src` path strings is lexical, so
  `zen_api-2025.10.0` sorted *before* `zen_api-2025.9.0` ('1' < '9') and
  `[-1]` ("newest is last") would silently pick the older one. Only latent
  today (one package version on disk) but would bite at the ZEN 3.14 upgrade
  once a second version is present. Fixed: sort by a parsed numeric version
  tuple (`_zen_api_src_version_key`) instead of the raw string.

- **A hardware-position lookup returning `None` crashed as a generic,
  misleading exception.** `MS_zenapi_objectivechanger.set_objective_set_optovar`
  dereferenced `get_objective_by_position`/`get_optovar_by_position`'s result
  via `.name` with no `None` check; an unmatched/transitional position raised
  a bare `AttributeError`, caught by `set_objective_set_optovar_sync`'s broad
  `except`/retry and burned through the whole retry budget as a generic
  "Exception during objective change" — masking what was really a
  config/hardware-state problem. Fixed: added `_require_objective`/
  `_require_optovar` helpers that raise a clear, descriptive `ValueError`
  instead.

- **Immersion objective position (`4`) was hardcoded twice.**
  `MS_CD7_API_LoA.set_objective_set_optovar_sync` re-hardcoded the literal `4`
  instead of importing `MS_zenapi_stage_LM.IMMERSION_OBJECTIVE_POSITION`, the
  documented single source of truth — a drift risk if the turret is ever
  reconfigured. Fixed: now references the shared constant.

- **(low) `fit_lsm_crop` could crash with `ZeroDivisionError`.**
  `MS_czexp_editor.fit_lsm_crop` divided by `target_fov_um` with no
  validation. Fixed: raises `ValueError` up front for a non-positive FOV.

- **(low) A same-file move could crash a smoke test.**
  `MS_zenapi_experiment_methods.py`'s snap-file move compared
  `snap_default_path != snap_custom_path` without `.resolve()`, unlike the
  equivalent comparison in `_move_result_and_cleanup` two functions over.
  Fixed: added the matching `.resolve()`.

- **(cleanup) Four copy-pasted range-validators in `MS_CD7_API_LoA.py`.**
  `validate_objective_number`/`validate_optovar_number`/`validate_z_position`/
  `validate_xy_position` repeated the same isinstance+range+message pattern.
  Collapsed into one `_validate_range(value, lo, hi, label, type_check,
  type_name, unit)` helper; public signatures/behavior unchanged (confirmed
  against `tests/unit/test_validation.py`).

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
| `ia_Marc_002` (OV nuclei, recentre, mid-plane; local) | `<prefix>_ov_summary.json` + `<well>/<prefix>_<well>_ov_nuclei.json`, `<tag>_recentre.json`, `<tag>_midplane.json` | `projects/Marc_SM_002/MS_Marc_SM_002.py` (local-only) |

Note: `targets.json` is the contract for analyses built from the ia_PoC_002 scaffold;
Marc's pair predates it and keeps its own names. Not enforced globally — each
producer/consumer pair just has to match.

**Shared IA code → `Image_Analysis/ia_common` (planned, decided 2026-09-29).**
The ia repos copy the same code between them:
- `common.py`: identical in ia_Marc and ia_NfS;
- scene centres from the actual stage positions: 4 copies;
- per-plane Z from the CZI: 3 copies;
- pixel → stage XY: 2 versions.

`ia_common` is a separate repo next to the ia repos and serves as the
**basis for the next ia project**. The existing ia repos are **not** adapted;
they are only sources to copy from. New projects use it as an editable pixi
path dependency; frozen projects pin a commit. Scope: CZI reading,
coordinate conventions, display helpers and the script/CLI contract of
`run_analysis`. The detection algorithms stay per project. The survey and
plan are in its own `TODO.md`.

**SmartMic does NOT depend on ia_common.** It keeps its own small CZI readers
in `MS_Helper_function` (stage/Z positions). They overlap with ia_common on
purpose, so that the library needs no image-analysis env.

### Dependency history: `zen_api_utils` vendored out

SmartMic originally relied on a handful of `zen_api_utils` helpers
(`initialize_zenapi`, `set_logging`, the objective/optovar position lookups, and the
SWAF demo helpers). `zen_api_utils` is hand-written example glue that ships only
inside the ZEN-API `python_examples` folder and is **not** a packaged dependency.
Those helpers were vendored into `MS_zenapi_helpers.py`, so the library now depends
only on the packaged `zen_api` gRPC stubs and no longer needs `zen_api_utils` on
`sys.path`. See `copyright.md` for the Zeiss attribution preserved during vendoring.
