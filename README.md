# MBAL tank runner

One command that samples tank volumes and runs an existing MBAL prediction
through OpenServer.

| File | Role |
|---|---|
| `mbal.py` | Command to run |
| `mbal_core.py` | Sampling, OpenServer, resume, and summaries |
| `example.yaml` | Small public template; copy it to a local config |
| `mbal_simple.py` | One deterministic run from a small YAML; no sampling |
| `mbal_ensemble.py` | Sample volumes and rerun the verified simple coupling N times |
| `simple.yaml` | Template for the simple runner |
| `docs/simple-runner.md` | Matching the official MBAL run |
| `docs/use-guide.md` | Volumes, controls, running, resume |
| `docs/mbal-openserver-runbook.md` | Licensed Windows/OpenServer checks |

Keep real model paths, object names, tags, priors, and results in the
gitignored `mbal_config.local.yaml`, never in `example.yaml`.

## Start here: match the official run

Before any sampling, prove that a prediction driven from Python equals the
same prediction run by hand in MBAL. `mbal_simple.py` does only that:

```bash
python mbal_simple.py simple.local.yaml --show     # resolved tags, no MBAL
python mbal_simple.py simple.local.yaml --check    # model volumes vs YAML
python mbal_simple.py simple.local.yaml --match    # official run vs YAML run
```

See [docs/simple-runner.md](docs/simple-runner.md). The probabilistic runner
below is unchanged, but its old per-tank `TRES[2]` result assumptions do not
match the model validated by `mbal_simple.py`. For this model, use the ensemble
layer on the verified simple runner:

```bash
python mbal_ensemble.py simple.local.yaml --dry-run --n 200
python mbal_ensemble.py simple.local.yaml --run --n 200
```

In `simple.local.yaml`, `stoiip` is the arithmetic mean. Add O&G
`p90_stoiip` (low) and `p10_stoiip` (high) together for an uncertain tank;
omit both to hold a tank fixed. The sampler uses independent tank dimensions,
fixed seed/LHS by default, and writes the field volume as the row-wise tank sum.
Successful rows resume safely; changed regenerated inputs are rejected. The
fitted prior is written to `ensemble_summary.csv`, and a run warns when the
three entered statistics are matched by more than one distribution.

## Legacy `mbal.py` volume contract

The sections below document the older generic probabilistic runner. It treats
`official_stoiip` as P50 and expects a different per-tank result hierarchy. It
remains useful for models where those tags have been verified, but it is not the
next step for the field-level result stream matched by `mbal_simple.py`.

Each tank requires `official_stoiip`, which is the **P50**.

- No P90/P10: the tank is **fixed** at `official_stoiip` in every realization,
  so its P90, P50, P10 and mean are all equal and every prediction result
  driven only by that tank is identical. The run names these tanks.
- Both P90/P10: a split lognormal reproduces all three entered values exactly
  and keeps the skew of an asymmetric range. Requires
  `0 < P90 < official < P10`.
- The mean is reported next to official. It matches official on a symmetric
  range and sits above it on a right-skewed one — that gap is the skew.
  official cannot be both the P50 and the mean unless the range is symmetric.
- `in_model: false` drops a tank entirely: not sampled, not written to MBAL,
  not in `stoiip_total`, not in the summary.
- Unknown keys inside a `tanks:` entry are rejected, so a misspelled
  `p90_stoiip` fails instead of silently becoming a fixed tank.
- Tanks are sampled independently with LHS or Monte Carlo, and
  `stoiip_total` is their row-wise sum.

## Controls

`controls:` is every MBAL input written before each prediction — gas lift,
injection rates, constraints, anything with an OpenServer access string. Each
entry takes a name, a literal tag, and either `value` (a constant) or `values`
(a list to sweep).

```yaml
controls:
  - name: gas_lift
    tag: MBAL.MB[0].PREDINP.CONSTRAINT[1].MAX_GASLIFT
    values: [0, 0.5, 1.0]        # swept
  - name: pred_watinj
    tag: MBAL.MB[0].PREDINP.WATINJ
    value: "YES"                 # constant
```

Tags are written verbatim; the runner has no built-in knowledge of what a
control means. Every swept control multiplies the row count and gets its own
paired `<name>_sensitivity.csv`. Copy access strings with Ctrl+Right-click in
MBAL; do not guess them.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# Windows + licensed MBAL only:
# pip install pywin32
```

```powershell
Copy-Item .\example.yaml .\mbal_config.local.yaml
git check-ignore -v -- .\mbal_config.local.yaml
python mbal.py --config .\mbal_config.local.yaml --validate-config
```

**MBAL must already be running before a licensed run.** OpenServer attaches to
a running MBAL and cannot start one. Open MBAL once, clear any startup dialog,
and leave it open. The runner leaves it open when it finishes; set
`close_mbal_on_finish: true` to shut it down at the end.

Built-in OpenServer tags cover tank volumes and results. Add a `tags:` mapping
only for version-specific overrides. In multi-tank predictions, verify
`result_index` because `TRES[2]` sheet 0 is commonly the consolidated result.

## Run

```bash
# Sample and report volumes; never open MBAL
python mbal.py --config mbal_config.local.yaml --dry-run --n 200

# Static config check
python mbal.py --config mbal_config.local.yaml --validate-config

# Windows smoke check: dispatch COM, open model, read inputs; no writes
python mbal.py --config mbal_config.local.yaml --check-openserver

# Base case: one run at the official volumes, no sampling
python mbal.py --config mbal_config.local.yaml --official-only

# Licensed prediction
python mbal.py --config mbal_config.local.yaml --n 200

# Discard an existing mbal_results.csv instead of resuming it
python mbal.py --config mbal_config.local.yaml --n 200 --fresh

# Sweep a configured control without editing the YAML
python mbal.py --config mbal_config.local.yaml --n 200 --control gas_lift=0,0.5,1.0

# Regenerate summaries from an existing results CSV
python mbal.py --config mbal_config.local.yaml --summarize-only
```

## Outputs and resume

| Artifact | Description |
|---|---|
| `samples_dry_run.csv` | Sampled inputs from `--dry-run` |
| `mbal_results.csv` | Resume-safe row-level prediction results |
| `summary_percentiles.csv` | Official, P90, P50, P10, mean, and standard deviation |
| `<name>_sensitivity.*` | Absolute and paired incremental field oil per swept control |
| `run_metadata.csv` | Seed, sampling method, swept controls, success counts |

On restart, rows with `status == ok` are skipped and failed rows are retried.
Stored input columns are checked against the deterministic sample table, so a
changed seed or prior cannot be resumed silently — that run stops with an
error. Use `--fresh` to discard the old `mbal_results.csv`, or point
`--out-dir` somewhere new.

Sensitivity CSVs compare each setting against the lowest value of the swept
control while holding other controls fixed. They report `delta_P90/P50/P10`,
`probability_delta_positive`, and `n_paired`, plus per-setting `n_expected`,
`n_ok`, `n_failed`, `n_missing`, and `success_fraction`. Incomplete settings
are logged and must be investigated before comparing percentiles.

## Verify

```bash
python -m pytest -q
ruff check *.py tests/
```
