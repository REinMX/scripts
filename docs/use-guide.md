# Use guide — MBAL open, then run

Windows + licensed MBAL + OpenServer. Python can live in this repo;
MBAL must be installed on the same machine.

For the complete work procedure (backup, private local config, printable tag
sheet, COM smoke test, minimal licensed run, resume, QA, and troubleshooting),
use [mbal-openserver-runbook.md](mbal-openserver-runbook.md). Never put private
model values in the committed `example.yaml`; copy that template to
`mbal_config.local.yaml`, which is ignored by Git.

Theory for the tank volumes: [oil-in-place.md](oil-in-place.md).

---

## 0. Once, with MBAL open

1. Open your `.mbi`.
2. Confirm a **prediction** is already set up (dates, wells, constraints).
   This script only calls `MBAL.MB.RunPrediction`. It does not build the
   prediction from scratch.
3. Write down the **exact** tank names and well names (case-sensitive).
4. Copy OpenServer strings from MBAL:
   - Put the mouse on the input (tank OOIP, prediction-constraint
     `MAX_GASLIFT`/`MAXINJWATRATE`, injector `CONSTFBHP`, …).
   - **Ctrl + Right-click** → copy the access string.
   - Paste that string into `tags:` in the YAML. `{tank}`, `{well}`,
     `{p}` stay as placeholders; names go in `tanks:` / `water_inj_well`
     / `gas_lift_well`.
5. Save the `.mbi`. Leave MBAL **closed** or let the script open the
   file (`MBAL.OPENFILE`). Do not edit the same file in the GUI while
   a run is going.

First-time Python (once per PC):

```text
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install pywin32
```

Always start with a **dry-run** (no MBAL). Only then run the licensed
sweep.

---

## Case 1 — Volume only (no prediction)

Use this when you change official volumes or `p_connected` and want the
decision table. MBAL does not need to be open.

1. Edit `official_stoiip` / `p_connected` in `mbal_config.local.yaml`.
2. Clear `gas_lift_values` and `water_inj_*_values` (or ignore extra rows).
3. Run:

```text
python mbal.py --config mbal_config.local.yaml --dry-run --n 800
```

4. Open `mbal_output/decision_volume_summary.csv`.
5. Check: P50 of the deeper sand is 0; base P50 is well below official
   A+B. If not, the prior is too high.
6. Check `P(all base tanks isolated)`. If it looks like the product of
   the individual P(connected) values, the shared barrier is not being
   modelled — see `connectivity_correlation` in
   [oil-in-place.md](oil-in-place.md#4-the-connected-volume-model).

---

## Case 2 — Producer prediction (A and B in the .mbi)

Sidetrack oil well, no lift sweep, no injector yet. Same three tanks in
YAML. Deeper sand C stays out of MBAL (`in_model: false`).

**In MBAL**

1. Tanks A and B exist and are linked to the oil producer.
2. Prediction is valid and runs by hand once (green, finishes).
3. Copy OOIP tags for A and B.
4. Verify tank result sheets in `TRES[2]`: sheet 0 is consolidated in a
   multi-tank case; tank sheets follow from 1. Set `result_index` if using
   numeric result tags.

**In YAML** (`mbal_config.local.yaml`)

1. `mbal_file:` → full path to the `.mbi`.
2. `tanks:` `name:` → exact MBAL tank names for A, B, and C.
3. Tank C: leave `in_model: false`.
4. Clear the sweep lists:

```yaml
gas_lift_values: []
water_inj_rate_values: []
water_inj_bhp_values: []
```

**Run**

```text
python mbal.py --config mbal_config.local.yaml --n 200
```

**Look at**

- `summary_percentiles.csv` — connected STOIIP
- `decision_volume_summary.csv` — base vs upside volume
- results CSV — `np_*`, `pres_*`, `rf_*` per tank

---

## Case 3 — Gas lift on the producer

Same as Case 2, plus a lift-rate grid.

**In MBAL**

1. Producer is a gas-lift well (`TYPE` includes lift).
2. Prediction has a gas-lift limit you can edit.
3. Ctrl+Right-click the prediction constraint → paste into
   `tags.gas_lift_rate`. The supported hierarchy is
   `PREDINP.CONSTRAINT[{p}].MAX_GASLIFT`.
4. `{p}` is the prediction-constraint row index (often `1`, but never
   assume it). Confirm it in the browser.

**In YAML** — same `mbal_config.local.yaml`, same three tanks

1. `gas_lift_values:` → rates in **model units** (e.g. MMscf/d), e.g.
   `[0, 0.5, 1.0, 1.5]`.
2. The default `MAX_GASLIFT` tag is field-level and does not use
   `gas_lift_well`; that name is only needed for a custom verified per-well tag.
3. Leave `water_inj_*_values` empty unless you also want that grid.

**Dry-run, then licensed**

```text
python mbal.py --config mbal_config.local.yaml --dry-run --n 50 --gas-lift-values 0,0.5,1.0

python mbal.py --config mbal_config.local.yaml --n 200
```

**Look at** `gas_lift_sensitivity.csv` / `.png` — field oil vs lift
rate. Do not read `np_total` from `summary_percentiles.csv` (that file
only repeats the tank volumes).

---

## Case 4 — Water injector (rate and BHP)

One injector linked to **both** tanks A and B. The script sets well
rate and BHP; MBAL splits the water.

**In MBAL**

1. Add a water injector, type `WATINJ`.
2. Link it to tank A and tank B.
3. Turn water injection on in prediction setup (`WATINJ = YES`).
4. Run the prediction once by hand with a dummy rate so you know it
   solves.
5. Ctrl+Right-click and copy:
   - prediction **maximum injection-water rate**
     (`PREDINP.CONSTRAINT[i].MAXINJWATRATE`) → `tags.water_inj_rate`
   - prediction **minimum injection-water rate**
     (`MININJWATRATE`) → `tags.water_inj_min_rate` for fixed-rate mode
   - injector **max FBHP** → `tags.water_inj_max_fbhp`
   - injector **constant FBHP** (if you use `control: bhp`) →
     `tags.water_inj_bhp`
6. Save.

**In YAML** — same `mbal_config.local.yaml`, same three tanks

1. `water_inj_well:` → exact injector name.
2. `water_inj_control:`
   - `rate_with_bhp_limit` — target rate, BHP is a cap (usual)
   - `rate` — pin min = max rate
   - `bhp` — fixed FBHP (`PERFORMTYPE=CFBHP`)
3. Lists in **model units**:

```yaml
water_inj_rate_values: [0, 300, 600]
water_inj_bhp_values: [250, 300]
```

Row count = `n_realizations` × rates × BHPs. Start small (`--n 20`)
until tags validate.

**Dry-run, then licensed**

```text
python mbal.py --config mbal_config.local.yaml --dry-run --n 20

python mbal.py --config mbal_config.local.yaml --n 200
```

If tag validation fails, the printed string is wrong for your IPM
version. Copy again from the browser; do not guess.

**Look at**

- `water_inj_sensitivity.csv` / `.png` — field oil vs rate (one line
  per BHP)
- `decision_volume_summary.csv` — volumes (same every rate)
- results CSV — `wi_*` is cumulative water injected if the tag exists

Optional TRES cumulative-water field names are deliberately not assumed. Add
`res_cumwat`/`res_cumwatinj` only after copying and probing those exact result
columns; otherwise `wp_*`/`wi_*` remain `NaN`.

---

## Case 5 — Deeper sand (tank C) in MBAL

Do this only after Case 1 still looks honest (P50(C) = 0).

**In MBAL**

1. Add tank C (deeper sand).
2. Link the oil producer to C (and the injector if you want support
   into that sand).
3. Run prediction once by hand.
4. Copy the OOIP tag for C.

**In YAML**

```yaml
  - key: C
    name: <exact MBAL tank name>
    index: 2
    official_stoiip: 6.5
    role: upside
    in_model: true
```

**Run** the same command as Case 2 or 4. C now gets an OOIP each
realization (0 is written as `min_tank_stoiip` so MBAL does not see a
true zero). `np_C` appears in the results.

---

## Case 6 — Official volumes changed

1. Change only `official_stoiip` on A / B / C.
2. Repeat Case 1 (dry-run). Check the decision table.
3. Repeat the licensed case you care about (2, 3, or 4).
4. Use a **new** `out_dir` (or delete the old CSV). Resume will refuse
   if the sampled volumes no longer match.

---

## If a licensed run stops

```text
python mbal.py --config mbal_config.local.yaml
```

Same `out_dir` / `out_csv` / seed / YAML. Failed rows are retried;
`status == ok` is skipped.

Rebuild plots from a finished CSV:

```text
python mbal.py --config mbal_config.local.yaml --summarize-only
```

---

## What to open after a run

| File | When |
|---|---|
| `decision_volume_summary.csv` | Any connected-volume run — base vs upside STOIIP |
| `summary_percentiles.csv` | Tank volume P90/P50/P10 |
| `gas_lift_sensitivity.csv` | Case 3 |
| `water_inj_sensitivity.csv` | Case 4 |
| `*_results.csv` | Row-level Np, pressure, RF |
| `mbal_run.log` | Errors and ETA |
