# Use guide — three tanks, then prediction controls

Use a Windows machine with licensed MBAL and OpenServer for predictions. A dry
run works anywhere and never opens MBAL.

For backup, COM, and tag troubleshooting details, use
[mbal-openserver-runbook.md](mbal-openserver-runbook.md).

## 1. Prepare MBAL once

1. Open the corrected `.mbi` and save a backup.
2. Confirm the prediction already runs manually. This script changes inputs and
   calls `MBAL.MB.RunPrediction`; it does not build the prediction.
3. Confirm all three tank names and their `TRES[2]` result sheets. Sheet 0 is
   commonly consolidated; tank sheets normally start at 1.
4. Copy any version-specific input/result strings with Ctrl+Right-click. The
   built-in tags are defaults; add only the overrides you need under `tags:`.
5. Do not edit the model in the GUI while OpenServer is running it.

First-time Python setup:

```text
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install pywin32
```

## 2. Create the local YAML

```powershell
Copy-Item .\example.yaml .\mbal_config.local.yaml
git check-ignore -v -- .\mbal_config.local.yaml
```

Set the model path and the exact three tank names. For each tank:

```yaml
- key: A
  name: <exact MBAL tank name>
  index: 0
  result_index: 1
  p90_stoiip: 3.5       # optional, but P90/P10 are a pair
  official_stoiip: 4.5  # fixed value, or the mean and P50 when P90/P10 exist
  p10_stoiip: 5.5
```

Omit both percentile fields for a fixed official volume. Do not add
connectivity, communication groups, volume scales, residual multipliers, or
base/upside roles; those keys are not part of this implementation.

Validate before sampling:

```text
python mbal.py --config mbal_config.local.yaml --validate-config
```

## 3. Volume dry run

```text
python mbal.py --config mbal_config.local.yaml --dry-run --n 200
```

Check:

- exactly 200 base realizations when all control lists are empty;
- one `stoiip_<key>` column per tank;
- `stoiip_total` equals the row-wise tank sum;
- fixed tanks equal `official_stoiip` in every row;
- probabilistic tanks reproduce official as the mean and P50, and reproduce the
  entered P90/P10 when those are symmetric about official;
- tanks with no P90/P10 show P90 = P50 = P10 = mean, which is expected;
- `summary_percentiles.csv` shows the official value beside each distribution.

## 4. Licensed producer prediction

Keep control lists empty:

```yaml
gas_lift_values: []
water_inj_rate_values: []
water_inj_bhp_values: []
```

Run a smoke check, then one realization, then the campaign:

```text
python mbal.py --config mbal_config.local.yaml --check-openserver
python mbal.py --config mbal_config.local.yaml --n 1
python mbal.py --config mbal_config.local.yaml --n 200
```

Review the results CSV for `np_*`, `pres_*`, and `rf_*` per tank and for the
field total.

## 5. Gas-lift sweep

Use rates in the model units:

```yaml
gas_lift_values: [0, 0.5, 1.0]
```

The default control is
`PREDINP.CONSTRAINT[{p}].MAX_GASLIFT`; verify `{p}` in MBAL. Every volume
realization is repeated at every lift rate. Read `gas_lift_sensitivity.csv` and
`.png`, not pooled production percentiles. The CSV also reports paired
incremental oil relative to the lowest lift rate for the same volume
realization and any other fixed control settings.

## 6. Water-injection sweep

Set the injector name if the chosen tags use `{well}`, then choose one control:

- `rate`: minimum and maximum rate are set to the same value;
- `bhp`: fixed FBHP with `PERFORMTYPE=CFBHP`;
- `rate_with_bhp_limit`: target rate plus maximum FBHP.

Example:

```yaml
water_inj_well: <exact injector name>
water_inj_control: rate_with_bhp_limit
water_inj_rate_values: [0, 300, 600]
water_inj_bhp_values: [250, 300]
```

Row count is `n_realizations × rates × BHPs`. Start with a small dry run. Read
`water_inj_sensitivity.csv` and `.png` for field oil by control setting. Paired
increments use the lowest swept rate, or the lowest BHP when BHP is the only
water control, as the reference while holding the other controls fixed.

Both sensitivity CSVs include:

- absolute Np P90/P50/P10 for each complete control setting;
- `delta_P90`, `delta_P50`, and `delta_P10` from paired realizations;
- `probability_delta_positive` and `n_paired`;
- `n_expected`, `n_rows`, `n_ok`, `n_failed`, `n_missing`, and
  `success_fraction`.

Do not compare settings with unexplained failures or missing realizations.
When three control axes are active together, the complete Cartesian results
remain in the CSV, but the two-dimensional sensitivity plot is skipped.

## 7. Resume and summarize

Restart with the same YAML, seed, and output directory:

```text
python mbal.py --config mbal_config.local.yaml
```

Rows with `status == ok` are skipped; failed rows are retried. Resume refuses a
CSV whose stored inputs differ from the regenerated sample table.

Rebuild summaries without opening MBAL:

```text
python mbal.py --config mbal_config.local.yaml --summarize-only
```

## Main outputs

| File | Purpose |
|---|---|
| `samples_dry_run.csv` | Sampled volumes and control grid |
| `summary_percentiles.csv` | Official, P90/P50/P10, mean, std for tank/field STOIIP |
| `mbal_results.csv` | Resume-safe row-level prediction results |
| `gas_lift_sensitivity.csv` | Absolute and paired incremental field Np by lift rate |
| `water_inj_sensitivity.csv` | Absolute and paired incremental field Np by injector rate/BHP |
| `mbal_run.log` | Errors, retries, and ETA |