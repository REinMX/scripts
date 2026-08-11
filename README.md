# Engineering scripts

## Probabilistic MBAL via OpenServer

`probabilistic_mbal_openserver.py` runs probabilistic Petroleum Experts MBAL
predictions through OpenServer.

The current design samples every MBAL tank independently. Field STOIIP is a
derived sum, not a prior used to constrain the tanks:

```text
STOIIP_A     ~ distribution A
STOIIP_B     ~ distribution B
STOIIP_total = STOIIP_A + STOIIP_B
```

### Configure tanks

Edit `_default_tanks()` in the script. Each tank has its own key, label, MBAL
index, STOIIP distribution, and optional aquifer-multiplier distribution:

```python
TankConfig(
    key="A",
    name="Upper reservoir",
    index=0,
    stoiip=Distribution(kind="lognormal", p90=20.0, p10=70.0),
    aquifer_multiplier=Distribution(kind="uniform", low=0.5, high=2.0),
)

TankConfig(
    key="B",
    name="Lower reservoir",
    index=1,
    stoiip=Distribution(kind="triangular", low=15.0, mode=45.0, high=90.0),
)
```

Supported distributions:

- `fixed`: `value`
- `uniform`: `low`, `high`
- `triangular`: `low`, `mode`, `high`
- `lognormal`: O&G low-case `p90`, high-case `p10`

Every non-fixed parameter receives a separate MC/LHS dimension. The dry run
prints the sample rank-correlation matrix; off-diagonal values should be near
zero for independent tank ranks.

### Run

```bash
# Sampling, summaries and plots only; does not open MBAL
python probabilistic_mbal_openserver.py --dry-run --n 1000

# Full Windows/OpenServer run
python probabilistic_mbal_openserver.py \
    --model 'C:\Work\Models\two_tank_model.mbi' \
    --n 500

# Rebuild summaries and plots from the existing result CSV
python probabilistic_mbal_openserver.py --summarize-only
```

Dependencies: `numpy`, `pandas`, `matplotlib`; `scipy` is optional; full MBAL
runs additionally require Windows, Petroleum Experts OpenServer, and `pywin32`.

The OpenServer tags in `TAGS` are placeholders because tag strings vary between
IPM versions. Copy each exact variable name from MBAL's OpenServer variable
browser before a licensed run.

### Verify

```bash
python -m pytest -q
ruff check probabilistic_mbal_openserver.py tests/
```
