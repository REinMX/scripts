# Simple runner

`mbal_simple.py` runs one MBAL prediction from one small YAML file. No
sampling, no percentiles, no plots: the numbers you type are the numbers that
get written, and one row of results comes back.

It exists to answer one question first — **does a prediction driven from
Python give the same answer as the same prediction run by hand in MBAL?**
Until that is yes, nothing built on top of it is worth reading.

The probabilistic runner (`mbal.py` / `mbal_core.py`) is unchanged and still
there.

## The config

```yaml
mbal_file: C:\Work\Models\three_tank_model.mbi

units:                 # what MBAL should answer in
  stoiip: MMstb
  oil: MMstb
  pressure: psia

tanks:                 # one official STOIIP per tank
  - name: Tank_A
    stoiip: 4.5
  - name: Tank_B
    stoiip: 3.0

controls:              # anything else written before the prediction
  - name: gas_lift
    tag: MBAL.MB[0].PREDINP.CONSTRAINT[1].MAX_GASLIFT
    value: 0.5
  - name: water_inj_rate
    tag: MBAL.MB[0].PREDINP.CONSTRAINT[1].MAXINJWATRATE
    value: 300

out_dir: simple_output
tolerance_pct: 0.1     # how close counts as a match
```

Tank names must match MBAL exactly. Control tags are written verbatim — copy
each one from MBAL with Ctrl+Right-click, never guess it. Unknown keys are
rejected, so a typo fails instead of being silently ignored.

Add `result_sheet: 1` to a tank if its `TRES[2]` sheet is addressed by number
rather than by name. Cumulative oil and tank pressure are read for every tank;
add anything else under `results:` with `{sheet}` and `{k}` placeholders.

## The five commands

```text
python mbal_simple.py simple.local.yaml --show       # resolved tags; no MBAL
python mbal_simple.py simple.local.yaml --check      # volumes in model vs YAML
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

**2. `--check`.** Reads the model's STOIIP and prints it beside yours.

```text
item      in MBAL   in YAML     diff   diff %  status
Tank_A          6       4.5     -1.5      -25  DIFF
Tank_B          3         3        0        0  MATCH
```

A `DIFF` here means one of two things, and both matter: your official numbers
are not the model's numbers, or the units differ. A 6.29 against a 1.0 is
MMstb against MMSm3, not a modelling disagreement. No writes happen in this
mode, so it is safe against a model you care about.

**3. `--match`.** Runs the prediction twice against the same model — once
exactly as saved, once with your YAML written in — and prints the two result
sets side by side.

```text
item          official   from YAML   diff   diff %  status
np_Tank_A        0.675       0.675      0        0  MATCH
pres_Tank_A      2,700       2,700      0        0  MATCH
np_total         1.125       1.125      0        0  MATCH
```

Keep `controls: []` for this first comparison, so volumes are the only thing
that could differ. Once it matches, add controls one at a time — from then on
every difference you see is the control doing something, not the coupling
being wrong.

Then run the same prediction by hand in MBAL and check the GUI's last-step
cumulative oil against `np_*`. That closes the loop: YAML equals script equals
GUI.

If `--run` reads back a value MBAL did not accept, it stops before predicting
rather than reporting a result from inputs that were never applied.

## Output

Each `--baseline` and `--run` appends one row to
`simple_output/simple_results.csv` with a timestamp, the mode, the inputs and
the results. Nothing is overwritten and nothing is resumed.
