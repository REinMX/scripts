# Statistics used by the MBAL runner

## O&G percentile convention

The output uses probability-of-exceedance labels:

| Label | Statistical percentile | Meaning |
|---|---:|---|
| P90 | 10th | low case; 90% of samples exceed it |
| P50 | 50th | median case |
| P10 | 90th | high case; 10% of samples exceed it |

Therefore a valid tank prior is ordered:

```text
P90 < P50 < P10
```

`official_stoiip` is both the mean and the P50 when P90/P10 are supplied. If
neither bound is supplied, the official value is fixed in every realization.

## Per-tank distribution

A probabilistic tank is sampled from a symmetric prior centred on
`official_stoiip`, with the standard deviation taken from the entered span:

```text
sigma = (P10 - P90) / (2 x 1.2816)
```

Symmetric P90/P10 are reproduced exactly. Asymmetric P90/P10 cannot be: a
distribution whose mean equals its median is symmetric by definition. The
entered span is preserved, `official_stoiip` stays the mean, and the run logs
a warning so the difference is never silent.

Samples are floored at `min_tank_stoiip`. With a span wide enough for that
floor to bite, the sample mean sits slightly above official and the run warns.

This is only an interpolation between the three volume cases. It is not a
communication, correlation, or geological dependency model.

## LHS and Monte Carlo

- `lhs`: stratifies each input dimension and usually gives stable marginal
  percentiles with fewer realizations.
- `mc`: independent pseudo-random draws from each input dimension.

Both methods use the configured seed and are reproducible. Each uncertain tank
has its own dimension, so the tank ranks are independent apart from finite
sample noise.

## Field volume

The field sample is calculated row by row:

```text
field[r] = tank_A[r] + tank_B[r] + tank_C[r]
```

Field P90/P50/P10 are calculated from that field column. Never calculate field
percentiles by adding tank percentiles; in general:

```text
P50(A + B) != P50(A) + P50(B)
```

## Mean and standard deviation

- Mean: arithmetic average across the samples.
- Standard deviation: spread of the sampled values around the mean.

Per tank, the sampled mean and P50 both reproduce `official_stoiip`, so either
can be compared against the official number.

Field totals are the row-wise sum of independent tanks, so the field mean is
the sum of the tank means. The field P50 is close to that sum but is not
guaranteed to equal it, and the field P90/P10 span is narrower than the sum of
the tank spans because independent tanks do not reach their low or high cases
together.

## Paired operational comparisons

Every operational setting reuses the same `base_realization`. Sensitivity
summaries therefore calculate incremental field oil row by row before taking
percentiles:

```text
delta_Np[r, setting] = Np[r, setting] - Np[r, reference]
```

The reference is the lowest value of the swept control while all other controls
are held fixed. `delta_P90/P50/P10` summarize these paired differences;
`probability_delta_positive` is the fraction of valid pairs with positive
incremental oil.

Each control setting also reports expected, present, successful, failed, and
missing row counts. Absolute or incremental percentiles can be biased when
settings do not contain the same realization population, so incomplete coverage
must be resolved rather than silently ignored.