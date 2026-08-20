# Use guide

Predictions need a Windows machine with licensed MBAL and OpenServer. A dry run
works anywhere and never opens MBAL.

For backup, COM, and tag troubleshooting, see
[mbal-openserver-runbook.md](mbal-openserver-runbook.md).

## 1. Prepare MBAL once

1. Open the corrected `.mbi` and save a backup.
2. Confirm the prediction already runs manually. This script changes inputs and
   calls `MBAL.MB.RunPrediction`; it does not build the prediction.
3. Confirm every tank name and its `TRES[2]` result sheet. Sheet 0 is commonly
   consolidated; tank sheets normally start at 1.
4. Copy every control tag, and any version-specific override, with
   Ctrl+Right-click. Do not guess access strings.
5. Do not edit the model in the GUI while OpenServer is running it.

**Leave MBAL open.** OpenServer attaches to a running MBAL and cannot start
one. Open it once, clear any startup dialog, and leave it running between
runs. The runner no longer shuts MBAL down; set `close_mbal_on_finish: true`
if you want the old behaviour.

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
python mbal.py --config .\mbal_config.local.yaml --validate-config
```

## 3. Volumes

Each tank needs `official_stoiip`. `p90_stoiip` and `p10_stoiip` are optional
but must be given together.

```yaml
- key: A
  name: <exact MBAL tank name>
  index: 0
  result_index: 1
  p90_stoiip: 3.5       # optional, paired with p10_stoiip
  official_stoiip: 4.5  # the P50
  p10_stoiip: 5.5
```

Add `in_model: false` to drop a tank entirely: it is not sampled, not written
to MBAL, and not counted in `stoiip_total`. Unknown keys inside a tank block
are rejected, so a misspelled `p90_stoiip` fails instead of silently turning
the tank into a fixed volume.

### What the prior does

`official_stoiip` is the **P50**. With P90 and P10 supplied, a split lognormal
calibrates the lower and upper log-space widths separately:

```text
sigma_low  = ln(official / P90) / 1.2816
sigma_high = ln(P10 / official) / 1.2816
```

All three entered values are reproduced exactly, and asymmetric ranges keep
their skew. Validation requires `0 < P90 < official < P10`.

The **mean** is reported next to official. On a symmetric range they match; on
a right-skewed range the mean sits above official, and that gap is the skew,
not an error:

| entered P90 / official / P10 | sampled P90 | P50 | P10 | mean |
|---|---|---|---|---|
| 3.5 / 4.5 / 5.5 | 3.50 | 4.50 | 5.50 | 4.50 |
| 3.5 / 4.5 / 6.5 | 3.50 | 4.50 | 6.50 | 4.81 |

official cannot be both the P50 and the mean unless the range is symmetric —
a distribution whose mean equals its median is symmetric by definition.

A tank with no P90/P10 is **fixed** at official in every realization. Its P90,
P50, P10 and mean are then all equal, and so is any prediction result driven
only by that tank. The volume table names these tanks so the flat percentiles
are never a mystery.

Tanks are sampled independently, one LHS or Monte Carlo dimension each. Field
STOIIP is the row-wise sum, and field percentiles come from that summed column
— never add tank percentiles, because percentiles of a sum are not the sum of
percentiles.

O&G convention throughout: P90 is the low case (10th statistical percentile),
P10 the high case (90th).

## 4. Controls

`controls:` is every MBAL input written before each prediction. Each entry has
a name, a literal OpenServer tag, and either `value` (a constant, written
unchanged every realization) or `values` (a list to sweep).

```yaml
controls:
  - name: gas_lift
    tag: MBAL.MB[0].PREDINP.CONSTRAINT[1].MAX_GASLIFT
    values: [0, 0.5, 1.0]

  - name: water_inj_rate
    tag: MBAL.MB[0].PREDINP.CONSTRAINT[1].MAXINJWATRATE
    values: [0, 300, 600]

  - name: water_inj_min_rate
    tag: MBAL.MB[0].PREDINP.CONSTRAINT[1].MININJWATRATE
    value: 0

  - name: pred_watinj
    tag: MBAL.MB[0].PREDINP.WATINJ
    value: "YES"
```

There is no built-in knowledge of what a control means — the tag you paste is
written verbatim. MBAL's own semantics are yours to express: pinning both
`MININJWATRATE` and `MAXINJWATRATE` gives a fixed rate, `PERFORMTYPE: "CFBHP"`
with `CONSTFBHP` gives fixed-BHP control, and a rate plus `MAXFBHP` gives a
rate target with a BHP limit. Anything else with an access string works the
same way.

Every swept control multiplies the row count: `n_realizations × values × ...`.
Each one is paired against the same volume realizations, and each gets its own
`<name>_sensitivity.csv`. Override a sweep from the command line without
editing the YAML:

```text
python mbal.py --config mbal_config.local.yaml --control gas_lift=0,1,2
```

## 5. Run

```text
python mbal.py --config mbal_config.local.yaml --validate-config   # static check
python mbal.py --config mbal_config.local.yaml --dry-run --n 200   # volumes only
python mbal.py --config mbal_config.local.yaml --check-openserver  # COM + tags
python mbal.py --config mbal_config.local.yaml --official-only     # base case
python mbal.py --config mbal_config.local.yaml --n 200             # campaign
```

On the dry run, check that `stoiip_total` is the row-wise tank sum, that fixed
tanks equal `official_stoiip` in every row, and that probabilistic tanks
reproduce the entered P90/P50/P10.

### Base case at the official volumes

`--official-only` runs a single deterministic realization with every tank
pinned to its `official_stoiip`. It ignores `p90_stoiip`/`p10_stoiip` and
forces `n_realizations` to 1, so the result is the one scenario your official
numbers describe — not a sample near it.

```text
python mbal.py --config mbal_config.local.yaml --official-only --dry-run
python mbal.py --config mbal_config.local.yaml --official-only
```

Swept controls still expand, so this is also how you run a control scenario
grid at the base volumes: three gas-lift rates gives three rows, one per rate,
all at official STOIIP. Use a separate `--out-dir` to keep the base case
beside the probabilistic campaign.

If a tank has a random aquifer distribution, the run warns: there is no
official aquifer number to pin, so it still takes a single random draw.

### Re-running into an existing output directory

The runner resumes by default: rows with `status == ok` are skipped. It
refuses to resume a results CSV whose stored inputs no longer match the
regenerated sample table, which is what you hit after changing the seed,
`n`, or a prior:

```text
cannot resume ...\mbal_results.csv: realization 0 has a different stoiip_A;
seed/distributions changed. Re-run with --fresh to discard it, or use a
different --out-dir.
```

That guard is deliberate — silently mixing two different priors into one CSV
would corrupt the percentiles. To start over, either point `--out-dir`
somewhere new, or discard the old results:

```text
python mbal.py --config mbal_config.local.yaml --n 200 --fresh
```

`--fresh` deletes only `mbal_results.csv` in `--out-dir`; summaries and plots
are rewritten by the run anyway.

## 6. Sensitivity output

Each `<name>_sensitivity.csv` compares every setting of that control against
its lowest value, holding all other controls fixed, and reports:

- absolute Np P90/P50/P10 for each complete setting;
- `delta_P90`, `delta_P50`, `delta_P10` from paired realizations;
- `probability_delta_positive` and `n_paired`;
- `n_expected`, `n_rows`, `n_ok`, `n_failed`, `n_missing`, `success_fraction`.

Do not compare settings with unexplained failures or missing realizations.
With three or more swept controls the full Cartesian results stay in the CSV
but the two-dimensional plot is skipped.

## 7. Resume and summarize

Restart with the same YAML, seed, and output directory. Rows with
`status == ok` are skipped and failed rows retried. Resume refuses a CSV whose
stored inputs differ from the regenerated sample table, so a changed seed or
prior cannot be resumed silently.

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
| `<name>_sensitivity.csv` | Absolute and paired incremental field Np per swept control |
| `run_metadata.csv` | Seed, sampling method, swept controls, success counts |
| `mbal_run.log` | Errors, retries, and ETA |
