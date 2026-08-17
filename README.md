# Engineering scripts

One maintained command and a public anonymized YAML template. Keep all real
model names, paths, tags, priors, and results in a gitignored local config.

| File | Role |
|------|------|
| `mbal.py` | The only command you run |
| `mbal_core.py` | Library (sampling, OpenServer, resume, summarize) |
| `example.yaml` | Public anonymized template; copy it, never put private values in it |
| `docs/mbal-openserver-runbook.md` | Full work runbook: open corrected MBAL model, collect tags, smoke test, run, resume |
| `docs/ipm-openserver-mbal-chapter-5-2025.md` | Saved 2025 Chapter 5 implementation reference supplied for this correction |
| `docs/use-guide.md` | Step-by-step with MBAL open, one section per case |
| `docs/oil-in-place.md` | Why official ≠ P50, connectivity, upside sand, how to present |
| `docs/statistics.md` | Every statistical term this prints, in plain words, and the traps |
| `probabilistic_mbal_openserver*.py` | Compatibility entry points that call the same maintained core |

**At work, start here:**
[MBAL/OpenServer runbook — corrected model to campaign](docs/mbal-openserver-runbook.md).
It starts by opening MBAL, loading the corrected `.mbi`, making a backup, and
copying the exact version-specific OpenServer strings without guessing.

## Private local configuration

Do not edit the committed template with work data:

```powershell
Copy-Item .\example.yaml .\mbal_config.local.yaml
git check-ignore -v -- .\mbal_config.local.yaml
python mbal.py --config .\mbal_config.local.yaml --validate-config
```

`*.local.yaml`, `.mbi` files, `work/`, and generated MBAL outputs are ignored.
Before any commit, still inspect `git diff`, `git diff --cached`, and search for
every private object name. Never commit asset identifiers or licensed results.

## Design

This is a **dynamic MBAL model**. Tank STOIIP is the oil the well sees in
each control volume, not a geological realization. Full theory, tank-by-tank
assumptions, and how to present the sidetrack as an upside (not a 14 MSm³
base) are in [docs/oil-in-place.md](docs/oil-in-place.md).

Working official numbers (4.5 / 3.0 / 6.5 MSm³) are placeholders. Change
`official_stoiip` when the mapped case updates; keep the structure.

**`connected_volume`** (this well). Official is the mapped *connected*
case. Discrete connectivity decides how much of that volume the well sees.
The deeper sand is `role: upside` and is off in P50.

```text
STOIIP_i     = official_i × connect_frac_i × residual_i
STOIIP_base  = A + B
STOIIP_total = base + optional deeper sand
```

Tanks isolated by the **same** barrier share a `connectivity.group`, and
`volume_model.connectivity_correlation` (0–1) sets how tightly the group
moves together. Each tank keeps its own `p_connected`; only the joint
case changes. Drawing one barrier as independent coin flips per tank is
false diversification — it narrows the field distribution and invents a
"exactly one sand connects" case. A and B share `base_sands` at 0.8; the
deeper sand C is a separate question and stays out.

### Operational sensitivities

Gas-lift rate, water-injection rate and injector BHP are **controls**, not
volume uncertainty. Each is a deterministic sweep paired across every
volume realization (Cartesian product if more than one list is set).

Add **one** water injector in the `.mbi` and link it to both tanks. The
2025 manual places water-rate controls in
`PREDINP.CONSTRAINT[i].MININJWATRATE/MAXINJWATRATE`; well BHP controls
remain in `PREDWELL[{well}].CONSTFBHP/MAXFBHP`. MBAL allocates between
tanks from injectivity and pressure. Gas-lift availability is
`PREDINP.CONSTRAINT[i].MAX_GASLIFT`. Copy the exact strings from MBAL's
browser and verify result fields/units for the installed version.

Step-by-step with MBAL open (volume-only, producer, gas lift, injector,
deeper sand): [docs/use-guide.md](docs/use-guide.md).

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# Windows + licensed MBAL:
# pip install pywin32
```

## Run

Enable a case by filling the matching list in your gitignored
`mbal_config.local.yaml` or on the command line.

```bash
# Volume table only (no MBAL)
python mbal.py --config mbal_config.local.yaml --dry-run --n 800

# Static readiness: no output, COM, or MBAL
python mbal.py --config mbal_config.local.yaml --validate-config

# Windows smoke test: open model and read configured inputs; no writes/prediction
python mbal.py --config mbal_config.local.yaml --check-openserver

# Licensed prediction (Windows)
python mbal.py --config mbal_config.local.yaml --n 200

# Gas lift on the same three tanks
python mbal.py --config mbal_config.local.yaml --dry-run --n 200 --gas-lift-values 0,0.5,1.0

# Water injector on the same three tanks
python mbal.py --config mbal_config.local.yaml --dry-run --n 200 \
    --water-inj-rate-values 0,300,600 --water-inj-bhp-values 250,300

python mbal.py --out-dir mbal_output --summarize-only
python mbal.py --write-example-config mbal_config.local.yaml
```

`summary_percentiles.csv` reports volumes once per volume realization.
`gas_lift_sensitivity.*` / `water_inj_sensitivity.*` are field oil vs the
control. `decision_volume_summary.csv` is base vs upside STOIIP.

`water_inj_control` is `rate`, `bhp` (fixed FBHP, `PERFORMTYPE=CFBHP`)
or `rate_with_bhp_limit` (target rate, `MAXFBHP` cap).

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

volume_model:
  kind: connected_volume      # the only model
  field_scale: { kind: lognormal, p90: 0.88, p10: 1.10 }
  residual: { kind: lognormal, p90: 0.85, p10: 1.12 }
  connectivity_correlation: 0.8   # within a connectivity group; 0 = independent

tanks:
  - key: A
    name: Upper
    index: 0
    result_index: 1             # TRES[2] sheet; sheet 0 is consolidated
    official_stoiip: 4.5      # working mapped connected-case; will change
    connectivity:
      { kind: two_section, p_connected: 0.30, isolated_fraction: 0.50,
        group: base_sands }
  - key: B
    name: Lower
    index: 1
    result_index: 2
    official_stoiip: 3.0
    connectivity:
      { kind: two_section, p_connected: 0.35, isolated_fraction: 0.50,
        group: base_sands }

tags:
  tank_stoiip: 'MBAL.MB[0].TANK[{i}].OOIP'
  gas_lift_rate: 'MBAL.MB[0].PREDINP.CONSTRAINT[{p}].MAX_GASLIFT'
  cmd_run_pred: MBAL.MB.RunPrediction
  res_nsteps: 'MBAL.MB[0].TRES[2][{r}].COUNT'
  # ...
```

Supported distributions: `fixed`, `uniform`, `triangular`, `lognormal` (O&G P90/P10).

One volume model: `connected_volume`. Each tank needs `official_stoiip`
plus `connectivity`. The older `fmu_residual` and `independent` models and
per-tank `stoiip:` draws were removed; configs using them fail with a
pointer to the replacement.

`TRES` prediction results use stream index `2`. In multi-tank cases, result
sheet `0` is consolidated and the following sheets are tanks; set
`tanks[].result_index` explicitly when numeric result tags are used. **Always**
copy exact OpenServer variable names from MBAL’s browser into `tags` before a
licensed run—especially result column names and unit qualifiers.

## Outputs

| Artifact | Description |
|----------|-------------|
| `*.csv` (results) | One row per realization; resume-safe; failed rows retried |
| `summary_percentiles.csv` | P95/P90/P50/P10/P5, mean, std |
| `run_metadata.csv` | Seed, sampling, success counts |
| `mbal_run.log` | Timestamped run log with ETA |
| `stoiip_*.png`, `*_per_tank.png` | Histograms + exceedance curves |
| `gas_lift_sensitivity.*` | Lift-rate field-oil table/plot (gas-lift entry only) |
| `water_inj_sensitivity.*` | Injector rate / BHP field-oil table/plot |
| `decision_volume_summary.csv` | Base vs upside vs total STOIIP, per-tank connectivity odds, and `P(all base tanks isolated)` (connected_volume) |

## Resume behaviour

- Only rows with `status == ok` are skipped on restart.
- Failed realizations are **retried**.
- Input columns are fingerprint-checked so a changed seed/prior cannot silently resume.

## Verify

```bash
python -m pytest -q
ruff check *.py tests/
```
