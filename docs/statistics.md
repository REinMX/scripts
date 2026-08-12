# The statistics in this model, in plain words

Every term this repo prints, what it means, where it comes from, and how
to say it out loud when someone asks. Written to be defended in a room,
not to be complete.

Theory for the volumes: [oil-in-place.md](oil-in-place.md).
How to run it: [use-guide.md](use-guide.md).

---

## 1. The one that causes arguments: P90 / P50 / P10

Oil and gas uses **exceedance** convention. A P90 is the number you have
a **90 % chance of beating**. So:

| Label | Means | Statistical percentile | Plain words |
|---|---|---:|---|
| **P90** | 90 % chance of exceeding | 10th | **low case** |
| **P50** | 50 % chance of exceeding | 50th | median |
| **P10** | 10 % chance of exceeding | 90th | **high case** |
| P95 | 95 % chance of exceeding | 5th | very low |
| P5 | 5 % chance of exceeding | 95th | very high |

In `mbal_core.percentiles()` this is literally `P90 = np.percentile(x, 10)`.

> **Say it out loud.** "P90 — the low case." Anyone with a statistics or
> data-science background reads P90 as the *high* end, because outside
> this industry a percentile counts upward. Naming the direction every
> time costs you two words and prevents the single most common
> misreading of this whole study.

**Exceedance curve** — the right-hand panel on every plot. The x-axis is
volume, the y-axis is P(X > x). Find 0.9 on the y-axis, read across, drop
down: that is your P90. It is just `1 − CDF`, and it is the honest way to
show a volume distribution because you read cases off it directly.

---

## 2. How a number becomes a distribution

**Realization** — one complete draw of every uncertain input. One row of
the results CSV. One MBAL prediction run. `n_realizations: 200` means 200
of them.

**Monte Carlo (MC)** — draw random inputs many times, run the model each
time, and look at the *distribution* of answers instead of one answer.

**Latin Hypercube Sampling (LHS)** — the default here (`sampling: lhs`).
Split each input's range into `n` equally-likely bins, draw exactly once
from each bin, then shuffle which bin of input A pairs with which bin of
input B. Same underlying distributions as plain MC, but the sample covers
the range evenly instead of leaving random gaps, so percentiles settle
down with far fewer runs. That matters when every run costs an MBAL
prediction.

> Caveat: LHS gives you *smoother* percentile estimates, not error bars
> you can quote. Don't read meaning into the third decimal.

**Seed** — fixes the pseudo-random stream so a run reproduces exactly
(`seed: 42`). Change the seed and the P-numbers move in the last digit or
two; that movement is sampling noise, not new information. The resume
logic fingerprints the sampled inputs precisely so a changed seed cannot
silently half-resume an old CSV.

**Inverse-CDF (PPF) sampling** — how every distribution here is actually
generated. Draw `u` uniformly between 0 and 1, then apply the inverse of
the cumulative distribution function. That is why each uncertain input
costs exactly one column of the "unit hypercube", and why LHS applied to
those uniform columns stratifies everything at once.

- **CDF** — F(x) = P(X ≤ x), the S-curve.
- **PPF / quantile function** — F⁻¹(u), the CDF read backwards: give it a
  probability, it returns the value.

---

## 3. The distribution families

| `kind` | Parameters | Use it when |
|---|---|---|
| `fixed` | `value` | No uncertainty. Costs no sample dimension. |
| `uniform` | `low`, `high` | You honestly know only the range. |
| `triangular` | `low`, `mode`, `high` | You have a most-likely value plus bounds and don't want to claim more shape than that. |
| `lognormal` | `p90`, `p10` | Volumes and multipliers. The default choice. |

**Why lognormal for volumes.** A volume is a *product* — area × thickness
× porosity × oil saturation ÷ Bo. Multiply several positive uncertain
factors and the result skews right and cannot go negative, which is what
a lognormal is: a variable whose **logarithm** is normal. It is the
volumetric equivalent of "errors multiply rather than add".

**Parameterised by P90/P10 on purpose.** Nobody can state the μ and σ of
a log. Everybody can state a low case and a high case.
`lognormal_from_p90_p10` converts:

```text
sigma = ln(P10 / P90) / (2 × 1.2816)      1.2816 = z at the 90th percentile
mu    = ½ × ln(P10 × P90)
```

A useful check: the median is `exp(mu)`, which equals `sqrt(P90 × P10)`,
the **geometric mean** of your two cases. For `p90: 0.88, p10: 1.10` that
is 0.9839 — slightly *below* 1.0. A P90/P10 pair that looks symmetric
around 1 is not centred on 1.

**Bernoulli / discrete draw** — connectivity is not a curve, it is a coin
flip with probability `p_connected`. It has only two outcomes, so it does
something the smooth families never do: it splits the answer in two.

---

## 4. Mixtures, and why the mean can be a lie

A **mixture distribution** is one made of distinct sub-populations. Tank
C is the clean example: it is off 75 % of the time and on 25 % of the
time, so its histogram is **bimodal** — a spike at zero and a separate
hump near 6.5.

From a 200 000-realization run of the shipped defaults:

```text
P(C = 0)                        0.750
median (P50) of C               0.000
mean of C                       1.577
P(C within ±10 % of its mean)   0.0000
```

The mean is 1.58 MSm³, and the tank is essentially **never** near 1.58.
It is a portfolio average across two futures, not a case that can happen.

> **Say it out loud.** "The mean is what you'd get on average if you
> drilled this prospect many times. It is not a case, and there is no
> realization that looks like it."

This is also why **median ≠ mean**. For a right-skewed lognormal the mean
sits above the median. For a bimodal mixture the mean can land in the
empty gap between the two modes. Quote P50 as the middle *case*; quote
the mean only when you genuinely want an expectation, e.g. for
risk-weighted economics.

**Standard deviation (`std`)** — average spread around the mean, reported
here with `ddof=0` (population, not sample). For skewed volumes it is
much less informative than simply quoting P90 and P10, which is why the
summary shows both.

---

## 5. Marginal, joint, and correlation

**Marginal distribution** — one variable considered on its own. "Tank A's
STOIIP" is a marginal. `p_connected: 0.30` is a marginal probability.

**Joint distribution** — how variables move *together*. Are both sands
isolated in the same realization, or does one compensate for the other?

This distinction carries the whole connectivity argument: coupling the
tanks changed the **joint** behaviour and left every **marginal**
untouched. A and B still connect 30 % and 35 % of the time. Only the
combinations changed.

**Correlation** — a number from −1 to +1 for how two variables move
together.

- **Pearson** correlation measures *linear* association, and skewed
  volumes distort it.
- **Rank (Spearman)** correlation measures whether they move in the same
  *direction*, ignoring shape. That is what the CLI prints
  (`samples[...].rank().corr()`), and it is the right one for volumes.

**Shared factor** — the simplest way to correlate things: draw one
multiplier per realization and apply it to several tanks. That is
`field_scale` — a common-mode error (Bo, one mapping bias) hitting A and
B together.

**Copula** — a way to impose a dependence structure while leaving each
variable's own distribution exactly as it was. The relevant sentence:
*a copula changes the joint, never the marginals*.

**One-factor Gaussian copula** — the specific one used for connectivity.
Each tank's coin flip is driven by a hidden standard-normal score:

```text
X_i = sqrt(rho) × Z + sqrt(1 - rho) × E_i
        ^ shared          ^ that tank's own noise
```

`Z` is shared by everything in the `connectivity.group`. The algebra
gives `corr(X_i, X_j) = rho` exactly, and each `X_i` is still standard
normal — which is *why* each tank's `p_connected` survives untouched. The
name is intimidating; the idea is "one common cause plus private noise",
the same structure banks use for correlated defaults.

**Comonotonic** — perfectly dependent, `rho = 1`: one draw decides
everyone. Then `P(all connected) = min(p_i)` and
`P(none connected) = 1 − max(p_i)`. It is the arithmetic bound, useful as
a sanity check even if you don't run at 1.0.

---

## 6. The portfolio effect (a.k.a. false diversification)

Add up several *independent* uncertain quantities and the total is
relatively **narrower** than its parts: the highs and lows partly cancel.
That is genuine and correct when the risks really are unrelated. It is
wrong — and flattering — when one geological feature drives all of them.

With the shipped `p_connected` of 0.30 and 0.35:

| Outcome | Independent | Shared barrier (0.8) |
|---|---:|---:|
| Neither sand connects | 0.453 | 0.582 |
| **Exactly one connects** | **0.444** | 0.187 |
| Both connect | 0.103 | 0.231 |

Independent draws put 44 % of the probability on "exactly one sand
connects" — the single most likely outcome in the study was a case that
makes no geological sense if one fault controls both sands. Correlating
them moves that mass out to the two ends, where the geology actually
lives.

---

## 7. Traps to avoid when presenting

**You cannot add P90s.** From a 40 000-realization run of the defaults:

```text
P90(A) + P90(B)   =  2.988      <- wrong, and conservative by accident
P90(A + B)        =  3.156      <- correct
```

The sum's low case does not require *both* tanks to hit their own low
case simultaneously. Always take percentiles of the summed column
(`stoiip_base`), never sum the per-tank percentile rows. The same applies
to P10 in the other direction.

**Percentiles of a ratio are not ratios of percentiles.** Recovery factor
is `Np / STOIIP`, both uncertain. P50(RF) ≠ P50(Np) / P50(STOIIP).
Compute the ratio inside each realization — which this code does — then
take percentiles of that column.

**More realizations does not mean more accuracy.** `n` reduces *sampling*
noise only. It does nothing about `p_connected` being a judgement call.
Going from 200 to 2 000 runs makes the numbers steadier, not truer.

**Correlation moves spread, not the mean.** If someone objects that
coupling the sands "made the project look worse", the mean is unchanged
by construction — 4.77 either way. The low case and the upside both got
more likely; the invented middle got less likely.

**Don't quote three significant figures.** A P50 of 4.088 MSm³ derives
from a `p_connected` someone chose in a meeting. Say "about 4",
and say which assumption would move it.

**Official is not P50.** The mapped number is a connected-case technical
volume. See [oil-in-place.md](oil-in-place.md) §2.

---

## 8. Where each term appears in the code

| Term | Where |
|---|---|
| O&G percentiles | `percentiles()`, `_extra_percentile_label()` |
| LHS / MC | `unit_hypercube()`, `sampling:` in YAML |
| Inverse-CDF sampling | `sample_distribution()`, `_norm_ppf()`, `_tri_ppf()` |
| Lognormal from P90/P10 | `lognormal_from_p90_p10()` |
| Bernoulli connectivity | `build_sample_table()`, `connected_*` columns |
| One-factor Gaussian copula | `_correlate_unit()`, `_norm_cdf()` |
| Shared factor | `field_scale` in `volume_model` |
| Rank correlation | printed by `main()` before the run |
| Joint connectivity odds | `decision_volume_summary.csv` |
