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

`official_stoiip` is used as P50 when P90/P10 are supplied. If neither bound
is supplied, the official value is fixed.

## Per-tank distribution

A probabilistic tank uses a split lognormal anchored at all three entered
points. Separate lower and upper log-space widths allow asymmetric distances
from P50 to P90 and P10 while keeping volumes positive.

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

For skewed distributions the mean need not equal P50. The official value should
be compared with P50, not with the mean.