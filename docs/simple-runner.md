# Simple runner

`mbal_simple.py` runs one MBAL prediction from one small YAML file. The numbers
you type are the numbers that get written, and one row of results comes back.
After that deterministic path matches, `mbal_ensemble.py` samples volumes and
reruns this same coupling without introducing a second tag/result model.

It exists to answer one question first — **does a prediction driven from
Python give the same answer as the same prediction run by hand in MBAL?**
Until that is yes, nothing built on top of it is worth reading.

The older probabilistic runner (`mbal.py` / `mbal_core.py`) is unchanged and
still there, but its per-tank result hierarchy has not matched this model. The
ensemble command documented below reuses the coupling that did match.

## The config

```yaml
mbal_file: C:\Work\Models\model.mbi

units: {}              # empty = whatever unit set the model is currently in

tanks:                 # stoiip required; the other two written only if given
  - name: OS-top
    stoiip: 4.5                  # -> TANK[{OS-top}].OOIP
    aquifer_volume: 120.0        # -> TANK[{OS-top}].AQUIF.VOLUME
    rock_compressibility: 3.5e-6 # -> TANK[{OS-top}].ROCKCOMPRESS
  - name: OS-bottom
    stoiip: 3.0

controls:              # anything else written before the prediction
  - name: gas_lift
    tag: MBAL.MB[0].PREDWELL[{OP-OS1}].GASLIFTRATE
    value: 0.5
  - name: winj_max_rate
    tag: MBAL.MB[0].PREDWELL[{WI-OS1}].CONSTRAINTS.MAXRATE
    value: 3000

results:
  stream: MBAL.MB[0].TRES[{Prediction}][{Prediction}]
  read:
    date: TIME
    cum_oil: CUMOIL
    res_pres: RESPRESS
  profile: false       # true also writes every time step to profile_*.csv

out_dir: simple_output
tolerance_pct: 0.1     # how close counts as a match
```

Tank and well names must match MBAL exactly. Control tags are written
verbatim — copy each one from MBAL with Ctrl+Right-click, never guess it.
Unknown keys are rejected, so a typo fails instead of being silently ignored.

Only three per-tank inputs have YAML keys, because they are the ones with a
fixed place in the tank object. Everything else — well PIs, injector limits,
constraints — is a control with its own tag, which is why `controls:` has no
built-in knowledge of what any of them mean.

### The results block

`stream` is the field-level prediction stream, addressed by name in this
model: `TRES[{Prediction}][{Prediction}]` is stream *Prediction*, sheet
*Prediction*. `read` maps the column name you want in the CSV to the MBAL
variable, written verbatim, so a unit qualifier goes here too:
`cum_oil: CUMOIL("MMstb")`.

The trailing index on a variable is the **row — the time step**, not the
variable. That is why a set of tags copied out of the results table carries
different numbers on the end:

```text
MBAL.MB[0].TRES[{Prediction}][{Prediction}][24].OILRATE
MBAL.MB[0].TRES[{Prediction}][{Prediction}][19].CUMOIL
```

24 and 19 are just the rows those cells happened to be on. The runner reads
`.COUNT` off the stream and uses `COUNT - 1`, so it always reports the end of
the prediction whatever the length. `profile: true` writes every step instead
of only the last.

## The five commands

```text
python mbal_simple.py simple.local.yaml --show       # resolved tags; no MBAL
python mbal_simple.py simple.local.yaml --check      # model inputs vs YAML
python mbal_simple.py simple.local.yaml --baseline   # official run, no writes
python mbal_simple.py simple.local.yaml --run        # write YAML, then predict
python mbal_simple.py simple.local.yaml --match      # baseline vs run
```

Every command that touches MBAL re-opens the model first, so a previous run
can never leak into the next one. MBAL must already be open: OpenServer
attaches to a running MBAL and cannot start one.

## Matching the official run

Work in this order.

**1. `--show`.** Read the tags out loud against MBAL's OpenServer browser.
Nothing is written until they are right.

**2. `--check`.** Reads the model's tank inputs and prints them beside yours.

```text
item              in MBAL   in YAML     diff   diff %  status
OS-top.OOIP             6       4.5     -1.5      -25  DIFF
OS-bottom.OOIP          3         3        0        0  MATCH
```

This is also how you fill the YAML in the first place: put anything in,
run `--check`, and copy the *in MBAL* column across.

A `DIFF` here means one of two things, and both matter: your official numbers
are not the model's numbers, or the units differ. A 6.29 against a 1.0 is
MMstb against MMSm3, not a modelling disagreement. No writes happen in this
mode, so it is safe against a model you care about.

**3. `--match`.** Runs the prediction twice against the same model — once
exactly as saved, once with your YAML written in — and prints the two result
sets side by side.

```text
item        official   from YAML   diff   diff %  status
cum_oil        1.125       1.125      0        0  MATCH
res_pres       2,700       2,700      0        0  MATCH
```

Keep `controls: []` for this first comparison, so volumes are the only thing
that could differ. Once it matches, add controls one at a time — from then on
every difference you see is the control doing something, not the coupling
being wrong.

Then run the same prediction by hand in MBAL and check the GUI's last row
against `cum_oil` and `res_pres`. That closes the loop: YAML equals script
equals GUI.

If `--run` reads back a value MBAL did not accept, it stops before predicting
rather than reporting a result from inputs that were never applied.

## Next step: N volume realizations

Add the ensemble controls and optional tank anchors to the same local YAML:

```yaml
n_realizations: 200
seed: 42
sampling: lhs

tanks:
  - name: OS-top
    stoiip: 4.5          # official arithmetic mean
    p90_stoiip: 3.5      # O&G low case, statistical 10th percentile
    p10_stoiip: 5.5      # O&G high case, statistical 90th percentile
  - name: OS-bottom
    stoiip: 3.0          # fixed when P90/P10 are absent
```

Inspect the realizations without touching MBAL, then run them:

```text
python mbal_ensemble.py simple.local.yaml --dry-run --n 200
python mbal_ensemble.py simple.local.yaml --run --n 200
```

The positive split-lognormal prior is calibrated to each entered P90,
arithmetic mean, and P10. Tanks use independent LHS/MC dimensions and
`stoiip_total` is summed row by row. If tank uncertainties share map/contact/
net-to-gross risks, independence is an assumption; provide a correlation model
before using the field spread for a decision.

The distribution each tank actually sampled is recorded in
`ensemble_summary.csv` as `fitted_median`, `fitted_sigma_low`,
`fitted_sigma_high` and `fitted_rivals`. Three statistics do not always pin
down one distribution: once P10/P90 is wide (roughly five or more), the mean
stops being monotone in the median and a second prior can reproduce the same
P90, mean and P10 with a visibly different spread. When that happens
`fitted_rivals` is non-zero and the run warns which median it sampled and which
it passed over. Narrow the anchors, or supply realizations, rather than
accepting the pick.

A tank with no P90/P10 is held fixed. If *no* tank has them, `--run` refuses
rather than spending N MBAL predictions on identical volumes; `--dry-run` warns
and still writes the (constant) sample table.

For a real campaign, provide per tank: the official arithmetic mean, O&G P90
(low), O&G P10 (high), and the common OOIP unit. Also choose `n`, the seed, and
whether tank marginals may be sampled independently. P50 is useful as a check
but is not required by this fitted prior. If GeoX can export row-level tank
realizations, prefer that table over three summary statistics: it preserves the
empirical tails and cross-tank dependence instead of fitting an assumed family.
Keep aquifer, rock, and operating controls at their verified simple-run values
unless they are deliberately added as separate uncertainties later.

The run writes `ensemble_samples.csv`, resume-safe `ensemble_results.csv`,
`ensemble_summary.csv`, and, when `results.profile: true`,
`ensemble_profiles.csv`. Existing `status == ok` rows are skipped; failures are
retried, as are nominally successful rows missing a required result. Resume
stops before opening MBAL if regenerated inputs differ. Use
`--fresh` to start a new campaign in the same output directory. After the last
row is durable, the runner reloads the saved model so MBAL is not left showing
an arbitrary realization's volumes.

## Deterministic output

Each `--baseline` and `--run` appends one row to
`simple_output/simple_results.csv` with a timestamp, the mode, the inputs and
the last-step results. Nothing is overwritten and nothing is resumed.

With `profile: true`, each run also writes `profile_baseline.csv` /
`profile_run.csv` — one row per time step, one column per variable in
`read:`. Those are overwritten each run.
