# Oil in place for this dynamic model

This is a **material-balance (MBAL) study**. The tanks are control volumes
in a dynamic model. They are not a geological model, a geomodel realization,
or an FMU ensemble of 3D grids.

STOIIP here means: *how much oil the well can see in each MBAL tank*.
That is smaller than, and more uncertain than, the mapped sand volume.

Working official numbers in the repo (4.5 / 3.0 / 6.5 MSm³) are
**placeholders**. Change `official_stoiip` when the mapped case is updated.
Do not rebuild the uncertainty structure just because the number moved.

The theory and the presentation below are written so this sidetrack is
treated as an **upside on a dying gas producer**, not as a standalone
field with 14 MSm³ in the title slide.

How to run each case with MBAL open: [use-guide.md](use-guide.md).
What every statistical term here means: [statistics.md](statistics.md).

---

## 1. What decision this model is for

A gas producer on this well will die. The oil sidetrack is a possible
incremental investment on that wellbore.

The decision is not “what is the STOIIP of the structure?”.

The decision is:

> If we sidetrack this well to oil, what connected oil does the well
> actually see, and is that enough — with injection and lift — to
> justify the sidetrack versus doing nothing?

Equinor language that actually helps here:

- **Upside** — incremental oil that is not in the current production
  forecast. This sidetrack *is* the upside. It is not the base of the
  field.
- **Derisk** — reduce the chance of a value-destroying decision. That
  does **not** mean inflate P50 until the project looks pretty. It means
  name the two or three uncertainties that flip drill / don’t-drill, and
  say what observation would move them.
- **Base / low / high** — connected volume the well sees today (base),
  the isolated / high-OWC case (low), and the deeper-sand-on case (high).
  Never one number.

If the study leads with 4.5 + 3.0 + 6.5 = 14 MSm³, it is selling a
mapped sum, not a decision.

---

## 2. Why official tank volumes are not the P50

An official / mapped tank volume is a **connected-case technical number**.
It usually assumes:

- the sand sections you drew as one tank actually communicate
- the oil-water contact you used is the one the well will see
- the deeper sand is either ignored or quietly left out

For this well those assumptions are the uncertainty, not a footnote.

A lognormal centred on official is optimistic in two ways:

1. For a right-skewed lognormal, **mean > median**. If official is the
   P50, expected STOIIP is already above official.
2. If two tanks are drawn independently, one being low is offset by the
   other being average. Shared Bo / mapping error does not work that
   way. Independent tanks **lift the field P90** (the low case looks
   better than it is). That is false diversification.

So official is an input *reference*, not a random variable and not the
mean.

---

## 3. The three tanks, as we understand them today

These descriptions are the reason the prior looks the way it does.
Update the *numbers* later; update the *structure* only if the story
changes.

### Tank A — official 4.5 MSm³

Two sections of the same sand, entered as one tank because the official
case assumes they communicate.

You said the well probably sees **much less — like half**. That is not a
wide lognormal around 4.5. It is a **discrete connectivity** question:

- connected: the well sees ~4.5 × residual
- isolated: the well sees ~2.25 × residual

`p_connected: 0.30` is a conservative working judgment (“probably much
less”), not a measurement. If you later believe communication is more
likely, raise `p_connected`. Do not raise official to make P50 look like
4.5.

### Tank B — official 3.0 MSm³

Smaller sand. Same connectivity issue as A, **plus oil-water contact
uncertainty**.

The residual on B is wider than on A on purpose (OWC moves volume
continuously). Connectivity is still discrete (`two_section`, half if
isolated). Do not fold OWC and connectivity into one generous lognormal
around 3.0 — that hides the isolated case in the left tail and keeps
the median too close to official.

### Tank C — official ~6.5 MSm³ (deeper sand)

Possibly connected to the same oil producer. More uncertain. This is
**upside**, not base.

- `role: upside`
- `connectivity.kind: optional` — the well sees all of it or none of it
- `p_connected: 0.25` — it does not belong in P50
- `in_model: false` until the tank exists in the `.mbi`

When C is off, sampled STOIIP is 0. The median of C is 0. The mean of C
is about `0.25 × 6.5 × E[residual]`. That is what an upside branch looks
like. Putting 6.5 in the P50 would be the optimistic study you asked
not to do.

C can stay in the YAML for the volume / decision table before it exists
in MBAL. Prediction (Np, pressure, injection allocation) only runs on
tanks with `in_model: true`.

---

## 4. The connected-volume model

```text
STOIIP_i = official_i × connect_frac_i × residual_i
           × field_scale     if role = base
           × 1               if role = upside

connect_frac_i =
    1                      if the well sees the official connected case
    isolated_fraction      if two_section and isolated   (0.5 for A, B)
    0                      if optional and not connected (C)

STOIIP_base   = STOIIP_A + STOIIP_B
STOIIP_upside = STOIIP_C
STOIIP_total  = STOIIP_base + STOIIP_upside
```

`field_scale` is a mild shared multiplier on A and B only (Bo, common
mapping bias). It is **not** a second copy of the connectivity story.
C does not share it — different sand, different contact, different
chance of being on the well.

There is no percentile anchor. The mixture already puts official on the
high side; rescaling it to sit at a chosen percentile would double-count
conservatism, or fight the discrete 0/1 on C.

### Connectivity is one barrier, not two coin flips

A and B are expected to be isolated by the **same** fault or shale. If
their connectivity is drawn independently, the dominant risk quietly
diversifies itself away: the most likely single outcome becomes "exactly
one sand connects", which is not a geological case anyone would defend.

`connectivity.group` puts tanks on a shared barrier, and
`volume_model.connectivity_correlation` (0 to 1) says how tightly they
are coupled. Each tank keeps its own `p_connected` exactly — only the
joint case moves. At correlation 1 the group collapses onto one draw, so
P(all connected) is the smallest `p_connected` in the group.

With the working defaults (`p_connected` 0.30 and 0.35):

| | independent (0.0) | shared barrier (0.8) |
|---|---:|---:|
| P(both sands connected) | 0.10 | 0.23 |
| P(neither connected) | 0.45 | 0.58 |
| Base P50 | 4.49 | 4.09 |
| Base P10 (high case) | 6.70 | 7.35 |
| Base mean | 4.77 | 4.77 |

The mean cannot move — the marginals are unchanged. What moves is the
**spread**: independent draws were manufacturing a middling case out of
two opposite geological outcomes. Correlating them makes both the low
case and the upside more likely, which is the honest shape for a
drill / don't-drill decision.

C is **not** in the group. Whether the deeper sand is present and
charged is a different question from whether A and B communicate.

`decision_volume_summary.csv` reports `P(all base tanks isolated)`. That
is the number to check: if it looks like the product of the individual
odds, the shared barrier is not being modelled.

### What you should see on a dry run

With the working defaults, roughly:

| Scope | Official | What P50 should do | What the mean should do |
|---|---:|---|---|
| A | 4.5 | sit near the isolated case (~2.2), not 4.5 | below 4.5 |
| B | 3.0 | below 3.0 | below 3.0 |
| C | 6.5 | **0** | a fraction of 6.5 |
| Base (A+B) | 7.5 | well below 7.5 | below 7.5 |
| Total | 14.0 | close to the base P50 (C is usually off) | base + ~0.25×C |

If P50 of the field is 14, or P50 of C is 6.5, the prior is wrong.

---

## 5. Models that used to be here

`connected_volume` is now the only volume model. Two others were removed:

- **`independent`** — each tank drew its own `stoiip:` distribution and
  the field was the sum. It diversified the shared risk away and made the
  field low case too high. That argument is now made properly by
  `connectivity_correlation` above, so the model is not needed to make it.
- **`fmu_residual`** — official × shared scale × residual, with an
  `official_as: p40` anchor. It is the right model *after* a test or
  interference data has settled communication, and the wrong one while
  “probably half” and “maybe the deep sand is on” are still the drivers.

If communication is later settled by data, the honest move is to set
`p_connected: 1.0` on the tanks that are shown to communicate and let the
residual carry the remaining uncertainty — same arithmetic, one model.

Old configs are not silently reinterpreted: a removed `volume_model.kind`
or a per-tank `stoiip:` fails validation with a pointer to the
replacement.

---

## 6. How this sits in MBAL

MBAL does not know about `p_connected`. It only knows the OOIP you
write on each tank for that realization.

- One water injector, linked to the tanks that exist in the `.mbi`.
  Rate and BHP are **controls**, swept the same way as gas lift. They
  are not volume uncertainty.
- If two sand sections communicate only weakly, the honest next MBAL
  step is two tanks plus a transmissibility — not one tank with 4.5.
  The `two_section` switch is the volume proxy until that model exists.
- If C is added to the `.mbi`, set `in_model: true` and link the
  producer (and injector, if you want support into that sand).
- Sampled volume of 0 is written as `min_tank_stoiip` (default 0.01) so
  MBAL does not see a zero OOIP. Recovery factor still uses the sampled
  0 and is NaN.

OpenServer names for the injector are from the Petex *IPM OpenServer
User Manual* (January 2011), MBAL `PREDWELL` / `PREDINP`. Copy the
exact strings from the browser for your IPM version.

---

## 7. How to present this (and how not to)

### Slide 1 — the decision

- Gas well dies. Sidetrack is incremental oil on that well.
- Three volume statements, not one:
  - **Low / isolated:** A and B at ~half official, C off.
  - **Base:** A and B as sampled (connectivity not assumed). C off.
  - **Upside:** C on, added to base.
- Injection and lift are sensitivities *on top of* those volumes.

### Slide 2 — do not lead with official

Do not put 4.5 / 3.0 / 6.5 in the first results table as if they were
P50. Put them in a footnote: *working mapped connected-case volumes,
to be updated*.

Use `decision_volume_summary.csv` from a dry run:

- base (sidetrack without deeper sand) P90 / P50 / P10 / mean
- upside sand only
- total including upside
- P(connected) per tank

### Slide 3 — what “derisk” means here

Derisk is a short list of observations that move `p_connected` or the
OWC residual, not a promise that the well is safe.

| Uncertainty | What would move it | What it does to the decision |
|---|---|---|
| A: two sections communicate | pressure / interference / MDT between the two sections | Moves A from ~2.25 toward 4.5 |
| B: OWC | contact, pressure gradient, water in test | Widens or tightens B continuously |
| B: connectivity | same as A | Isolated B is ~1.5, not 3.0 |
| C: on the producer | pressure communication to the deeper sand, or a penetration | Turns 0 into ~6.5 × residual. This is the upside branch. |
| Injection BHP / rate | facility / well design, not subsurface data | Recovery on whatever volume is connected |

A sidetrack off a dying gas well is often robust at a modest P50
because incremental cost is low. The study should show **P90 incremental
oil vs do-nothing**, not a decorated mean of 14 MSm³.

If P90 incremental oil still works, the well is already derisked in the
only sense that matters for this decision. If it does not, the derisk
action is “get communication / contact data”, not “add the deep sand to
the base case”.

### What not to say

- “Geological realizations.”
- “P50 STOIIP is 7.5 MSm³” while official is 4.5 + 3.0 and connectivity
  is unresolved.
- “There is also 6.5 MSm³ of upside” in the same breath as the base,
  without showing P(C connected) and P50(C) = 0.
- “We used a conservative lognormal.” A lognormal around official is
  not conservative.

---

## 8. How the study should be improved from here

In order. Do not do 4 before 1.

1. **Update official volumes in YAML only.** Keep the three-tank
   connected-volume structure. The placeholders are supposed to move.
2. **Build the do-nothing case.** Same well, gas producer dies, no
   sidetrack. Incremental Np = sidetrack − do-nothing. That is the
   decision metric, not tank OOIP.
3. **Split A into two tanks + transmissibility** in MBAL when you can.
   The `two_section` flag is a volume stand-in for that model.
4. **Add C to the `.mbi` when you are ready** and set `in_model: true`.
   Until then the upside lives in the volume table only.
5. **Keep injection and lift as grids**, paired with every volume
   sample. Do not average Np across injection rates.
6. **Calibrate `p_connected` in public.** Write down why A is 0.30 and
   C is 0.25. When someone disagrees, change the probability, re-run
   `--dry-run`, and show the decision table again. That is the study.
7. **Do not add more residual width** to look more “uncertain”. Extra
   lognormal noise around 4.5 hides the isolated case. If you are more
   unsure, lower `p_connected` or widen the OWC residual on B only.

---

## 9. How to change the official volumes later

```yaml
tanks:
  - key: A
    official_stoiip:  <new mapped connected-case A>
  - key: B
    official_stoiip:  <new mapped connected-case B>
  - key: C
    official_stoiip:  <new mapped deeper-sand case>
```

Leave `isolated_fraction: 0.50` unless “half” is no longer the isolated
story. Leave `role: upside` on C unless the deeper sand is no longer
optional. Re-run:

```bash
python mbal.py --config mbal_config.local.yaml --dry-run --n 800
```

Check that P50(C) is still 0 and that base P50 is still well below
official A+B. Then, and only then, run the licensed MBAL sweep.

---

## 10. References used for the implementation

- Petroleum Experts, *IPM OpenServer User Manual* (January 2011), Part 5
  MBAL: `PREDINP`, `PREDWELL` (`TYPE=WATINJ`, `MAXRATE`, `MINRATE`,
  `CONSTFBHP`, `MAXFBHP`, `PERFORMTYPE`, `CONSTRAINT.MAXINJWATRATE`).
- Equinor FMU / ERT residual practice: a **global volume multiplier**
  plus a **local residual**, not independent tank totals treated as
  independent geomodel outcomes. Applied here only as `field_scale` on
  the base tanks. Connectivity is extra, because this well’s main
  uncertainty is whether the official tank is the volume the well sees.
- Decision quality, not reserves booking: P90 / P50 / P10 of
  *connected* volume and incremental production, with the upside sand
  on a separate branch.
