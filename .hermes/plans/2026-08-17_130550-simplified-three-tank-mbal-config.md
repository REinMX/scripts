# Simplified Three-Tank MBAL Configuration Implementation Plan

> **For Hermes:** Use test-driven-development to implement this plan. Do not commit unless Javier asks.

**Goal:** Replace the current connectivity/communication-based STOIIP configuration with a small, working three-tank YAML: one official volume per tank, optional P90/P10 volume bounds, `n_realizations` sampling, and the existing prediction-control sweeps.

**Architecture:** Keep the proven MBAL/OpenServer, tag formatting, result reading, resume, and gas-lift/water-injection paths. Replace only the volume model with an independent per-tank prior. A tank is fixed at its official volume when P90/P10 are omitted; when both are present, official is treated as P50 and the sampler matches the requested P90/P50/P10.

**Tech stack:** Python 3, dataclasses, NumPy, pandas, PyYAML, pytest; optional SciPy for LHS/normal quantiles; Windows COM/pywin32 for the licensed MBAL run.

---

## Scope and decisions

### Keep

- Exactly three configured MBAL tanks, with `key`, `name`, `index`, and `result_index`.
- `n_realizations`, `seed`, and `sampling` (`lhs` or `mc`).
- Existing gas-lift and water-injection prediction controls:
  - `gas_lift_values`
  - `water_inj_rate_values`
  - `water_inj_bhp_values`
  - `water_inj_control`
- Existing OpenServer model opening, input writes, prediction command, result reads, failure rows, resume checks, and sensitivity summaries.
- Optional `in_model` and aquifer inputs because they affect MBAL execution, not the STOIIP uncertainty model. Omit them from the minimal example unless needed.
- Default OpenServer tags in Python. YAML should contain only tag overrides required by the installed MBAL version.

### Remove from the volume path

- `volume_model`
- `Connectivity` and all `connectivity.*` fields
- connectivity groups, correlation, and `--connectivity-correlation`
- `field_scale`, residual volume multipliers, discrete communication draws
- `role: base/upside`, `stoiip_base`, `stoiip_upside`, connectivity diagnostics, and `decision_volume_summary.csv`
- connectivity-specific plots, warnings, and documentation

### Sampling contract

For each tank:

1. `official_stoiip` is required, positive, and is the deterministic official case.
2. If `p90_stoiip` and `p10_stoiip` are both omitted, every realization uses `official_stoiip`.
3. If uncertainty is requested, both are required and must satisfy:
   `0 < p90_stoiip < official_stoiip < p10_stoiip`.
4. In the uncertain case, treat official as P50. Use one independent unit-hypercube dimension per tank and a two-sided lognormal transform:
   - lower-side sigma calibrated from official/P90;
   - upper-side sigma calibrated from P10/official;
   - continuous at official and exactly anchored at P90/P50/P10.
5. Sample tanks independently. Derive `stoiip_total` as the row-wise sum. Do not sample a field total or split fractions.
6. Deterministic prediction sweeps remain paired across every base volume realization. Total run rows remain:
   `n_realizations × product(number of enabled control values)`.

**Confirmed assumption (2026-08-17):** For this first working version, “official” is the central/P50 volume when P90/P10 are supplied.

---

## Target YAML

The committed `example.yaml` and `--write-example-config` output should be approximately:

```yaml
mbal_file: C:\Work\Models\three_tank_model.mbi
tag_mode: name

n_realizations: 200
seed: 42
sampling: lhs
out_dir: mbal_output

tanks:
  - key: A
    name: REPLACE_WITH_TANK_A_NAME
    index: 0
    result_index: 1
    official_stoiip: 4.5
    p90_stoiip: 3.5       # optional; omit P90 and P10 for a fixed volume
    p10_stoiip: 5.5

  - key: B
    name: REPLACE_WITH_TANK_B_NAME
    index: 1
    result_index: 2
    official_stoiip: 3.0  # fixed because P90/P10 are omitted

  - key: C
    name: REPLACE_WITH_TANK_C_NAME
    index: 2
    result_index: 3
    official_stoiip: 6.5
    p90_stoiip: 5.0
    p10_stoiip: 8.0

gas_lift_values: []
water_inj_control: rate_with_bhp_limit
water_inj_rate_values: []
water_inj_bhp_values: []

# Add only MBAL-version-specific overrides when the built-in tags do not match:
# tags:
#   res_cumoil: '<exact Ctrl+Right-click string from this MBAL version>'
```

Do not introduce a second YAML layer or a generic variable engine in this iteration. The small single file is the shortest route to a usable campaign.

---

### Task 1: Lock the new volume contract with tests

**Objective:** Define fixed and uncertain tank behavior before changing production code.

**Files:**
- Create: `tests/test_simple_volume_sampling.py`
- Delete after replacement: `tests/test_volume_model.py`
- Modify helpers in:
  - `tests/test_probabilistic_mbal_openserver.py`
  - `tests/test_probabilistic_mbal_openserver_gas_lift.py`
  - `tests/test_water_inj.py`

**Steps:**

1. Add a test that a tank with only `official_stoiip` produces exactly that value for every realization.
2. Add a large-LHS test showing an uncertain tank reproduces its configured P90, official/P50, and P10 within a small tolerance.
3. Make that test asymmetric around official so the implementation cannot silently use the old symmetric lognormal fit.
4. Add a test that three tank columns exist and `stoiip_total == stoiip_A + stoiip_B + stoiip_C` row by row.
5. Add a test that independently uncertain tanks have near-zero rank correlation at large `n`.
6. Add validation tests for:
   - only one of P90/P10 present;
   - non-positive values;
   - incorrect ordering;
   - duplicate keys/indices;
   - non-positive realization count.
7. Add migration-safety tests proving old `volume_model`, `connectivity`, `residual`, and `role` YAML keys fail clearly instead of being silently ignored.
8. Update operational-sweep test fixtures to construct simple tanks while preserving all gas-lift/water-injection assertions.

**Verification:**

Run the new test module first and expect failures caused by the not-yet-implemented fields/sampler:

```bash
.venv/bin/python -m pytest tests/test_simple_volume_sampling.py -q
```

---

### Task 2: Simplify the dataclasses and YAML parser

**Objective:** Make the in-memory model match the minimal YAML and reject the removed model explicitly.

**Files:**
- Modify: `mbal_core.py:45-258` (types, dataclasses, three-tank defaults)
- Modify: `mbal_core.py:376-683` (YAML parsing and serialization)
- Modify: `mbal_core.py:736-1020` (validation)
- Modify: `mbal_core.py:2519-2674` (CLI)
- Modify: `mbal_core.py:2834-2868` (exports)

**Steps:**

1. Add `p90_stoiip: float | None` and `p10_stoiip: float | None` to `TankConfig`.
2. Remove `Connectivity`, `VolumeModel`, `TankRole`, `ConnectivityKind`, and `Config.volume_model`.
3. Remove `role`, `residual`, and `connectivity` from `TankConfig`.
4. Keep the three default tanks, but make their STOIIP values direct fixed/quantile inputs. All three should default to `in_model: true`; a private YAML can set one false explicitly if its `.mbi` does not contain that tank.
5. Parse and serialize the two optional percentile fields.
6. Add strict legacy-key detection at top level and per tank. Raise an actionable message such as: “connectivity volume modelling was removed; use official_stoiip with optional p90_stoiip/p10_stoiip.”
7. Validate both-or-neither percentile fields and strict ordering around official.
8. Remove `--connectivity-correlation` and its CLI override code.
9. Keep the current prediction-control CLI flags unchanged.
10. Change config serialization so built-in default tags are omitted; serialize only tag overrides. This keeps `--write-example-config` short without losing custom MBAL tags.

**Verification:**

```bash
.venv/bin/python -m pytest tests/test_simple_volume_sampling.py -q
.venv/bin/python -m pytest tests/test_probabilistic_mbal_openserver.py -q
```

---

### Task 3: Replace connected-volume sampling with direct tank sampling

**Objective:** Generate independent per-tank STOIIP realizations and a field sum with no hidden communication logic.

**Files:**
- Modify: `mbal_core.py:1072-1385` (distribution transform and `build_sample_table`)
- Modify: `mbal_core.py:1707-1806` (stable row schema and resume input columns)

**Steps:**

1. Add a small transform for the optional P90/P50/P10 prior using the existing normal inverse-CDF helper.
2. Count one random dimension only for tanks that have P90/P10.
3. For each tank, write only the audit/input columns:
   - `official_<key>`
   - `stoiip_<key>`
4. Remove `field_scale`, `residual_*`, `connected_*`, and `connect_frac_*` columns.
5. Derive `stoiip_total` row by row.
6. Retain `stoiip_in_model` only when needed for total recovery factor with an `in_model: false` tank.
7. Sample aquifer distributions after the tank-volume dimensions as today.
8. Keep operational sweep expansion unchanged so every control setting sees the same base realization.
9. Update resume fingerprint columns to the new minimal schema and ensure a changed official/P90/P10/seed cannot resume an old campaign.

**Verification:**

```bash
.venv/bin/python -m pytest tests/test_simple_volume_sampling.py tests/test_probabilistic_mbal_openserver.py -q
```

Expected behavior:

- fixed tanks are constant;
- sampled per-tank P90/P50/P10 match the YAML;
- field volume is always the row-wise sum;
- old campaign CSVs fail resume with a clear schema mismatch.

---

### Task 4: Preserve the working MBAL prediction path

**Objective:** Ensure simplifying volume inputs does not alter OpenServer writes or prediction sensitivities.

**Files:**
- Modify only as required: `mbal_core.py:1476-1704` (input writes/results)
- Modify: `tests/test_probabilistic_mbal_openserver.py`
- Modify: `tests/test_probabilistic_mbal_openserver_gas_lift.py`
- Modify: `tests/test_water_inj.py`

**Steps:**

1. Keep writing `stoiip_<key>` to each tank’s existing OOIP tag.
2. Keep name/index tag modes and `result_index` behavior unchanged.
3. Keep gas-lift and water-injection Cartesian expansion unchanged.
4. Keep the stable result row, per-tank Np/pressure/RF, field Np/RF, retry, and resume behavior unchanged.
5. Verify that every `base_realization` retains identical tank volumes across all control settings.
6. Verify that all three in-model tanks are written once per realization.
7. Do not change OpenServer tag strings or prediction result semantics in this refactor.

**Verification:**

```bash
.venv/bin/python -m pytest \
  tests/test_probabilistic_mbal_openserver.py \
  tests/test_probabilistic_mbal_openserver_gas_lift.py \
  tests/test_water_inj.py -q
```

---

### Task 5: Simplify summaries, example YAML, and documentation

**Objective:** Make the normal workflow “edit three volumes, choose n, optionally set controls, run.”

**Files:**
- Modify: `example.yaml`
- Modify: `mbal.py`
- Modify: `README.md`
- Modify: `docs/use-guide.md`
- Modify: `docs/mbal-openserver-runbook.md`
- Replace/simplify: `docs/oil-in-place.md`
- Replace/simplify: `docs/statistics.md`
- Modify: `mbal_core.py:1949-2493` (summaries/plots/diagnostics)

**Steps:**

1. Replace `example.yaml` with the minimal schema shown above.
2. Make `--write-example-config` produce the same short schema, not every default/tag.
3. Remove connectivity/base/upside diagnostics and `decision_volume_summary.csv`.
4. Keep one `summary_percentiles.csv` containing each tank and field total. Add an `official` column for STOIIP rows so official, sampled P90/P50/P10, and mean can be compared in one place.
5. Default output to P90/P50/P10; remove P95/P5 from the default user-facing report. Keep mean/std only if already needed by generic prediction summaries.
6. Retain generic tank/field histograms only if they require no special volume logic; they are optional and must not block a run. Do not add new volume plots.
7. Rewrite the README’s volume section to four rules: official, optional P90/P10, independent tanks, field sum.
8. Rewrite the use guide around:
   - fixed official case (`n=1` recommended);
   - uncertain volume dry-run;
   - producer prediction;
   - gas lift;
   - water injection;
   - changing official/P90/P10 and starting a new output directory.
9. Keep the runbook’s OpenServer safety procedure, but remove communication/connectivity instructions.
10. State clearly that `tags:` is optional in YAML and only necessary for MBAL-version-specific overrides copied from the variable browser.

**Verification:**

```bash
.venv/bin/python mbal.py --config example.yaml --dry-run --n 200
```

Expected artifacts:

- `mbal_output/samples_dry_run.csv`
- `mbal_output/summary_percentiles.csv`
- `mbal_output/run_metadata.csv`
- optional generic PNGs if matplotlib is installed
- no `decision_volume_summary.csv`

---

### Task 6: Full verification and licensed smoke-run handoff

**Objective:** Finish with a demonstrably running local workflow and a bounded Windows MBAL check.

**Files:** No new production files unless verification finds a defect.

**Steps:**

1. Run the full local test suite.
2. Run Ruff.
3. Generate a fresh example YAML and load it back.
4. Execute a 200-realization dry-run from the committed example.
5. Inspect the CSV programmatically and assert:
   - exactly 200 base rows when all control lists are empty;
   - three tank STOIIP columns;
   - field total equals their sum;
   - fixed B equals official in every row;
   - uncertain A/C quantiles are close to their configured values;
   - no connectivity/residual/field-scale columns exist.
6. On the licensed Windows machine, copy the minimal example to `mbal_config.local.yaml`, fill private model/tank values, then run:

```powershell
python mbal.py --config .\mbal_config.local.yaml --validate-config
python mbal.py --config .\mbal_config.local.yaml --check-openserver
python mbal.py --config .\mbal_config.local.yaml --n 1 --out-dir smoke_output
python mbal.py --config .\mbal_config.local.yaml --n 20 --out-dir small_campaign
```

7. Confirm the one-realization run writes all three OOIP tags, runs `MBAL.MB.RunPrediction`, and reads the final TRES row for every tank before scaling up.

**Final local quality gates:**

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check *.py tests/
.venv/bin/python mbal.py --config example.yaml --dry-run --n 200
```

---

## Risks and trade-offs

- **Meaning of official:** Confirmed for this first version: official is P50 when P90/P10 are supplied. Revisit the distribution contract later if official becomes only a reference case.
- **Independent tanks:** Removing communication deliberately assumes independent tank volume priors. This is simple and transparent, but it can narrow field uncertainty when tanks actually share geological risks. That is accepted for this first working version.
- **Breaking config change:** Existing connected-volume YAMLs and output CSVs are incompatible. Fail loudly; do not silently reinterpret them. Use a new `out_dir` after migration.
- **Fixed volumes with `n > 1`:** This repeats the same MBAL case and wastes licensed runtime. Document `n=1` for all-fixed volumes; do not add automatic collapsing in this iteration.
- **Version-specific MBAL tags:** Omitting default tags from the YAML makes it shorter, but the Windows smoke check remains mandatory. Any installed-version differences belong in a small `tags:` override block.
- **Licensed verification:** Linux can fully verify sampling, YAML, summaries, and dry-run outputs. COM/MBAL prediction execution must be verified on the licensed Windows host.

## Acceptance criteria

- A user changes three official volumes in one short YAML without touching connectivity, communication, correlation, residual, role, or volume-model fields.
- Each tank can independently be fixed or use optional P90/P50/P10 uncertainty.
- `n_realizations` reproducibly samples the requested tank distributions.
- Field STOIIP is the row-wise sum of the three tanks.
- Existing gas-lift and water-injection prediction controls still work and remain paired to each volume realization.
- The local full test suite, Ruff, and a real 200-realization dry-run pass.
- A one-realization licensed Windows smoke campaign writes all three tank volumes and returns prediction results.
