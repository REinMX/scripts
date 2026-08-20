# MBAL three-tank runner

One maintained command for sampling three tank volumes and running an existing
MBAL prediction through OpenServer.

| File | Role |
|---|---|
| `mbal.py` | Command to run |
| `mbal_core.py` | Sampling, OpenServer, resume, and summaries |
| `example.yaml` | Small public template; copy it to a local config |
| `docs/use-guide.md` | Dry-run, prediction, lift, and injection workflow |
| `docs/mbal-openserver-runbook.md` | Licensed Windows/OpenServer checks |

Keep real model paths, object names, priors, and results in the gitignored
`mbal_config.local.yaml`, never in `example.yaml`.

## Volume model

Each tank requires `official_stoiip`, which is both the **mean** and the
**P50** of that tank's prior.

- No P90/P10: the tank is **fixed** at `official_stoiip` in every realization,
  so its P90, P50, P10 and mean are all equal and every prediction result
  driven only by that tank is identical across realizations. The run prints a
  warning naming these tanks.
- Both P90/P10: sample a prior centred on `official_stoiip`, with the P90-P10
  span setting the spread. Symmetric P90/P10 are reproduced exactly; asymmetric
  ones keep `official_stoiip` as the mean and preserve the span, and the run
  warns that the sampled P90/P10 will differ from the entered values.
- P90 and P10 must be supplied together and satisfy
  `0 < P90 < official < P10`.
- `in_model: false` drops a tank entirely: not sampled, not written to MBAL,
  not in `stoiip_total`, not in the summary.
- Unknown keys inside a `tanks:` entry are rejected, so a misspelled
  `p90_stoiip` fails instead of silently becoming a fixed tank.
- Tanks are sampled independently with LHS or Monte Carlo.
- Field STOIIP is calculated for every row:

```text
stoiip_total = stoiip_A + stoiip_B + stoiip_C
```

There is no connectivity, grouping, field-scale, residual multiplier, or
base/upside volume adjustment. Old keys are rejected rather than silently
reinterpreted.

Gas lift, water-injection rate, and injector BHP remain prediction controls.
They are deterministic sweeps paired with every sampled volume realization.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# Windows + licensed MBAL only:
# pip install pywin32
```

## Local configuration

```powershell
Copy-Item .\example.yaml .\mbal_config.local.yaml
git check-ignore -v -- .\mbal_config.local.yaml
python mbal.py --config .\mbal_config.local.yaml --validate-config
```

Normal YAML:

```yaml
mbal_file: C:\Work\Models\three_tank_model.mbi
tag_mode: name
n_realizations: 200
seed: 42
sampling: lhs
out_dir: mbal_output

gas_lift_values: []
water_inj_control: rate_with_bhp_limit
water_inj_rate_values: []
water_inj_bhp_values: []

tanks:
  - key: A
    name: TANK_A
    index: 0
    result_index: 1
    p90_stoiip: 3.5
    official_stoiip: 4.5
    p10_stoiip: 5.5
  - key: B
    name: TANK_B
    index: 1
    result_index: 2
    official_stoiip: 3.0
  - key: C
    name: TANK_C
    index: 2
    result_index: 3
    p90_stoiip: 5.0
    official_stoiip: 6.5
    p10_stoiip: 8.0
```

**MBAL must already be running before a licensed run.** OpenServer attaches to
a running MBAL and cannot start one. Open MBAL once, clear any startup dialog,
and leave it open. The runner leaves it open when it finishes; set
`close_mbal_on_finish: true` only if you want MBAL shut down at the end.

Built-in OpenServer tags cover the normal MBAL hierarchy. Add a `tags:` mapping
only for version-specific overrides. Copy access strings with Ctrl+Right-click
in MBAL; do not guess them. In multi-tank predictions, verify `result_index`
because `TRES[2]` sheet 0 is commonly the consolidated result.

## Run

```bash
# Sample and report volumes; never open MBAL
python mbal.py --config mbal_config.local.yaml --dry-run --n 200

# Static config check
python mbal.py --config mbal_config.local.yaml --validate-config

# Windows smoke check: dispatch COM, open model, read inputs; no writes
python mbal.py --config mbal_config.local.yaml --check-openserver

# Licensed prediction
python mbal.py --config mbal_config.local.yaml --n 200

# Optional control sweeps
python mbal.py --config mbal_config.local.yaml --n 200 \
  --gas-lift-values 0,0.5,1.0
python mbal.py --config mbal_config.local.yaml --n 200 \
  --water-inj-rate-values 0,300,600 --water-inj-bhp-values 250,300

# Regenerate summaries from an existing results CSV
python mbal.py --config mbal_config.local.yaml --summarize-only
```

`water_inj_control` may be `rate`, `bhp`, or `rate_with_bhp_limit`.

## Outputs and resume

| Artifact | Description |
|---|---|
| `samples_dry_run.csv` | Sampled inputs from `--dry-run` |
| `mbal_results.csv` | Resume-safe row-level prediction results |
| `summary_percentiles.csv` | Official, P90, P50, P10, mean, and standard deviation |
| `gas_lift_sensitivity.*` | Absolute and paired incremental field oil by gas-lift rate |
| `water_inj_sensitivity.*` | Absolute and paired incremental field oil by injector rate/BHP |
| `run_metadata.csv` | Seed, sampling method, and success counts |

On restart, rows with `status == ok` are skipped and failed rows are retried.
Stored input columns are checked against the deterministic sample table, so a
changed seed or prior cannot be resumed silently.

Operational sensitivity CSVs compare each setting against the lowest value of
the swept control while holding any other controls fixed. They report
`delta_P90/P50/P10`, `probability_delta_positive`, and `n_paired`, plus
per-setting `n_expected`, `n_ok`, `n_failed`, `n_missing`, and
`success_fraction`. Incomplete settings are logged and must be investigated
before comparing percentiles.

## Verify

```bash
python -m pytest -q
ruff check *.py tests/
```