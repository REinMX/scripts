# Oil in place used by the MBAL runner

This implementation deliberately uses a small volume model. It does not try to
represent communication, contacts, shared mapping uncertainty, or geological
dependencies yet. The objective is to get three tanks running reliably through
sampling and MBAL prediction.

## Per-tank inputs

Every tank requires:

```yaml
official_stoiip: 4.5
```

This is a fixed value unless both probabilistic bounds are supplied:

```yaml
p90_stoiip: 3.5
official_stoiip: 4.5   # interpreted as P50
p10_stoiip: 5.5
```

The O&G convention is used:

- P90: low case, 90% probability of exceedance.
- P50: median case.
- P10: high case, 10% probability of exceedance.

Validation requires:

```text
0 < P90 < official < P10
```

P90 and P10 must be present together. Supplying only one is an error.

## Sampling

For a probabilistic tank, the code samples a symmetric prior centred on the
official volume, so official is both the mean and the P50. The standard
deviation comes from the entered span, `sigma = (P10 - P90) / (2 x 1.2816)`.
Symmetric P90/P10 are reproduced exactly; asymmetric ones keep official as the
mean and preserve the span, and the run warns that the sampled P90/P10 differ
from the entered values.

A tank with no P90/P10 is fixed at official in every realization.

Each tank receives its own LHS or Monte Carlo dimension. No rank, random draw,
or multiplier is shared between tanks.

For realization `r`:

```text
field_stoiip[r] = tank_A[r] + tank_B[r] + tank_C[r]
```

Field P90/P50/P10 are then calculated from the sampled `field_stoiip` column.
Do not add tank percentiles; percentiles of a sum are not the sum of the
percentiles.

## What is intentionally absent

The current volume model has no:

- connectivity probability or communication group;
- cross-tank correlation;
- field-level scale factor;
- residual multiplier;
- base/upside volume branch;
- field-total rescaling.

Legacy YAML keys for those concepts are rejected instead of ignored. If a later
decision requires shared geological uncertainty, add it as an explicit model
change with new tests rather than hiding it in the current priors.

## MBAL and prediction controls

The sampled tank values are written to each configured MBAL tank OOIP input.
Gas lift, water-injection rate, and injector BHP are independent operational
controls: their grids are paired with every volume realization and do not alter
the sampled volume prior.

The main volume output is `summary_percentiles.csv`, which reports the official
value beside P90/P50/P10, mean, and standard deviation for each tank and the
field sum.