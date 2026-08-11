# Engineering scripts

Flat layout for probabilistic Petroleum Experts **MBAL** runs via **OpenServer**.

| File | Role |
|------|------|
| `mbal_core.py` | Shared library (sampling, OpenServer, resume, summarize, YAML, CLI) |
| `probabilistic_mbal_openserver.py` | Entry: independent per-tank MC/LHS |
| `probabilistic_mbal_openserver_gas_lift.py` | Entry: same + gas-lift sensitivity sweep |
| `example_config.yaml` | Starter config (index-based tags) |
| `example_gas_lift_config.yaml` | Starter config (name-based tags + lift) |

## Design

Every tank has its **own** STOIIP distribution and independent sample dimension.
Field STOIIP is a **derived sum**, not a prior:

```text
STOIIP_A     ~ distribution A
STOIIP_B     ~ distribution B
STOIIP_total = STOIIP_A + STOIIP_B
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# Windows + licensed MBAL:
# pip install pywin32
```

## Run

```bash
# Sampling only (macOS/Linux/Windows; no MBAL)
python probabilistic_mbal_openserver.py --dry-run --n 1000

# From YAML
python probabilistic_mbal_openserver.py --config example_config.yaml --dry-run

# Full OpenServer run (Windows)
python probabilistic_mbal_openserver.py \
    --config example_config.yaml \
    --model 'C:\Work\Models\two_tank_model.mbi' \
    --n 500

# Rebuild summaries/plots from an existing results CSV
python probabilistic_mbal_openserver.py --out-dir mbal_mc_output --summarize-only

# Write a fresh example YAML
python probabilistic_mbal_openserver.py --write-example-config my_field.yaml
```

### Gas-lift sensitivity

```bash
python probabilistic_mbal_openserver_gas_lift.py \
    --dry-run --n 200 --gas-lift-values 0,0.5,1.0,1.5

python probabilistic_mbal_openserver_gas_lift.py \
    --config example_gas_lift_config.yaml --dry-run
```

For a gas-lift sweep, `summary_percentiles.csv` reports geological inputs once per
base realization. Use `gas_lift_sensitivity.csv` / `.png` for field oil vs lift rate.

## YAML config

Tanks, distributions, OpenServer tags, and run controls can live outside Python:

```yaml
mbal_file: C:\Work\Models\field.mbi
tag_mode: index          # index | name
n_realizations: 500
seed: 42
sampling: lhs            # lhs | mc
out_dir: mbal_mc_output
validate_tags: true
reconnect_every: 0       # reopen model every N ok runs (0 = never)
log_level: INFO

tanks:
  - key: A
    name: Upper
    index: 0
    stoiip: { kind: lognormal, p90: 20, p10: 70 }
    aquifer_multiplier: { kind: uniform, low: 0.5, high: 2.0 }

tags:
  tank_stoiip: 'MBAL.MB[0].TANK[{i}].OIIP("{u}")'
  # ...
```

Supported distributions: `fixed`, `uniform`, `triangular`, `lognormal` (O&G P90/P10).

**Always** copy exact OpenServer variable names from MBAL’s browser into `tags`
before a licensed run — strings differ by IPM version.

## Outputs

| Artifact | Description |
|----------|-------------|
| `*.csv` (results) | One row per realization; resume-safe; failed rows retried |
| `summary_percentiles.csv` | P95/P90/P50/P10/P5, mean, std |
| `run_metadata.csv` | Seed, sampling, success counts |
| `mbal_run.log` | Timestamped run log with ETA |
| `stoiip_*.png`, `*_per_tank.png` | Histograms + exceedance curves |
| `gas_lift_sensitivity.*` | Lift-rate field-oil table/plot (gas-lift entry only) |

## Resume behaviour

- Only rows with `status == ok` are skipped on restart.
- Failed realizations are **retried**.
- Input columns are fingerprint-checked so a changed seed/prior cannot silently resume.

## Verify

```bash
python -m pytest -q
ruff check *.py tests/
```
